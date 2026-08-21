"""会话状态机（第 2.3 / 3.3 / 9.2 节）。

管理会话列表、当前活跃会话、消息流、Agent 运行状态。
处理流式输出时的会话切换保护（session_id 校验）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from .. import constants as C

# 直接从子模块导入，避免走 api/__init__.py → dsh_service → core/__init__.py 的循环链路
from ..api.dsh_service import DshService, MessageRecord, SessionInfo
from ..api.ws_client import WSEventType, WSMessage
from ..utils.logger import get_logger

if TYPE_CHECKING:
    WSEventMessage = WSMessage

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
    # 扩展字段：本地工具拦截执行时写入 callId / 视觉模型信息 / 耗时等
    extra: dict[str, Any] = field(default_factory=dict)


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
    # 最近一轮对话的消耗（元），用于余额内联小部件展示「本轮 ¥X」
    last_turn_cost: float = 0.0
    # 本会话累计消耗（元），切换会话时归零
    session_total_cost: float = 0.0
    # 本 Turn 的思考内容（reasoning 块累积，Web 版 ThinkRow 数据源）
    pending_reasoning: str = ""
    # 本 Turn 的队列/计划/审批快照（Web 版高级交互数据源）
    pending_todos: list[dict] = field(default_factory=list)
    pending_queue: list[dict] = field(default_factory=list)
    pending_approval: dict | None = None
    # 兜底轮询锚点：发送消息 / turn_start 时记录，_poll_once 只接受其后产生的消息
    # （防止 Agent 忙时消息被排队、轮询把上一个 turn 的旧回复当本轮显示）
    _poll_anchor_ts: float = 0.0
    # 消息时间戳索引：加速历史增量去重（O(1) 查询 vs 全表扫描）
    _msg_ts_index: set[float] = field(default_factory=set)
    # 文件变更追踪器（首次调用 snapshot_files 时懒加载）
    _file_tracker: Any = None  # FileChangeTracker | None，延迟导入避免循环


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

        # ===== 兜底轮询：RUNNING 状态下如果 WebSocket 丢失事件，定期 get_history 拉取 =====
        # 启动阈值：状态进入 RUNNING/THINKING/TOOL_EXECUTING 后若 X 秒无任何通知，启动轮询
        self._poll_interval = 5.0     # 轮询间隔（秒），5 秒一次，足够轻量
        self._poll_timeout = 120.0    # 最大轮询时长（2 分钟），超时自动标记 IDLE
        self._poll_lock = threading.Lock()
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        # session_id -> sent_epoch（该 session 何时进入 RUNNING，用于 2 分钟超时判定）
        self._poll_running_since: dict[str, float] = {}

    def _ensure_poll_running(self) -> None:
        """启动兜底轮询线程（只启动一次）。"""
        with self._poll_lock:
            if self._poll_thread is not None and self._poll_thread.is_alive():
                return
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="session-man-poll",
            )
            self._poll_thread.start()

    def _stop_poll(self) -> None:
        with self._poll_lock:
            if self._poll_thread is not None:
                self._poll_stop.set()
                t = self._poll_thread
                self._poll_thread = None
            else:
                t = None
        if t is not None:
            try:
                t.join(timeout=3)
            except Exception:
                pass

    def _mark_session_poll_start(self, session_id: str) -> None:
        """标记一个会话进入需要轮询的状态。"""
        self._poll_running_since[session_id] = time.time()
        self._ensure_poll_running()

    def _mark_session_poll_end(self, session_id: str) -> None:
        """标记会话结束（恢复 IDLE），停止对该会话的轮询。"""
        self._poll_running_since.pop(session_id, None)

    def _poll_loop(self) -> None:
        """轮询主循环：扫描所有进入 RUNNING 的会话，get_history 合并，直到都结束。"""
        import time as _t
        while not self._poll_stop.is_set():
            if not self._poll_running_since:
                # 没有需要轮询的会话：直接退出，下次 send_message 再重启
                with self._poll_lock:
                    if not self._poll_running_since:
                        self._poll_thread = None
                        return
            # 快照需要轮询的会话（避免迭代期间修改）
            pending = list(self._poll_running_since.keys())
            now = _t.time()
            for sid in pending:
                started_at = self._poll_running_since.get(sid)
                if started_at is None:
                    continue
                # 超过 2 分钟无结果：强制标记 IDLE，避免永久轮询
                if now - started_at > self._poll_timeout:
                    state = self._sessions.get(sid)
                    if state and state.agent_status != AgentStatus.IDLE:
                        log.warning("轮询超时 session=%s，强制 IDLE", sid)
                        state.agent_status = AgentStatus.IDLE
                        state.last_event_type = "error"
                        state.last_event_data = {
                            "message": "Agent 响应超时（> 2 分钟无事件），请检查网络或重试",
                            "error": "poll_timeout",
                        }
                        state.streaming_buffer = ""
                        self._mark_session_poll_end(sid)
                        self._notify(sid)
                    continue
                try:
                    self._poll_once(sid)
                except Exception as e:
                    log.debug("轮询 session=%s 异常: %s", sid, e)
            # 等待一轮，允许中途被 stop 打断
            if self._poll_stop.wait(self._poll_interval):
                return

    def _poll_once(self, session_id: str) -> None:
        """单次轮询：拉取历史，合并新消息，检测是否出现 assistant 结尾（TURN 完成）。"""
        state = self._sessions.get(session_id)
        if state is None:
            self._mark_session_poll_end(session_id)
            return
        # 如果 WebSocket 已经让它恢复到 IDLE，停止轮询
        if state.agent_status == AgentStatus.IDLE:
            self._mark_session_poll_end(session_id)
            return
        hist = self.dsh.get_history(session_id) or []
        if not hist:
            return

        # 锚点过滤：只接受轮询启动后产生的消息。
        # 场景：Agent 忙时用户新消息被排队（无新 turn），此时 history 里仍含
        # 上一个 turn 的 assistant 回复；若不过滤，轮询会把旧回复当作本轮
        # 结束显示，造成"回复是上一个问题的"。
        anchor = getattr(state, "_poll_anchor_ts", 0.0)
        if anchor > 0:
            hist = [m for m in hist if m.timestamp >= anchor - 2.0]
            if not hist:
                return

        # ---- 二次检查：get_history() 期间 WebSocket 可能已处理完 TURN_END ----
        # 如果 agent 已恢复 IDLE，轮询不再处理，避免与 WebSocket 重复添加消息。
        if state.agent_status == AgentStatus.IDLE:
            self._mark_session_poll_end(session_id)
            return

        # 先检测"本轮结束候选"（hist 里最后一条非空 assistant），
        # 必须在 _append_messages 之前做去重判断 —— 合并会把消息加入
        # state.messages，之后再查 already_has 恒为 True，ended 永远不触发。
        finalized: MessageRecord | None = None
        for m in reversed(hist):
            if getattr(m, "role", None) == "assistant" and getattr(m, "content", ""):
                finalized = m
                break
        already_has = finalized is not None and any(
            sm.role == "assistant" and sm.content == finalized.content
            for sm in state.messages
        )

        # 去重合并
        added = self._append_messages(state, hist)
        state.messages.sort(key=lambda m: m.timestamp)

        # 只有 finalized 是"本轮新增"（合并前 state.messages 中不存在）才算
        # 本轮有效结束；若内容早已存在（上一个 turn 的回复），不触发 turn_end，
        # 避免把旧回复补进当前流式行（"回复是上一个问题的"）。
        ended = False
        if finalized is not None and not already_has:
            if state.streaming_buffer and finalized.content.startswith(state.streaming_buffer):
                # WS 收到了一部分，剩下的在历史里：补齐
                state.streaming_buffer = finalized.content
            elif not state.streaming_buffer:
                state.streaming_buffer = finalized.content
            ended = True

        if added or ended:
            state.last_event_type = "history_sync" if not ended else "turn_end"
            if ended:
                # turn_end：固化 streaming 缓冲 + 切 IDLE
                if state.streaming_buffer:
                    # 已经加过 finalized，这里避免重复追加
                    state.streaming_buffer = ""
                state.agent_status = AgentStatus.IDLE
                self._mark_session_poll_end(session_id)
                state.last_event_data = {"message": finalized}
            else:
                state.last_event_data = {"added": len(added)}
            self._notify(session_id)

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

    def _get_state(self, session_id: str) -> SessionState | None:
        """获取指定会话的运行时状态。"""
        if not session_id:
            return None
        return self._sessions.get(session_id)

    def add_listener(self, listener: Callable[[str, SessionState], None]) -> None:
        """注册会话状态变更监听器。"""
        self._listeners.append(listener)

    def _notify(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state:
            event_type = getattr(state, "last_event_type", "")
            log.info("[DEBUG] _notify: session=%s event_type=%s agent_status=%s listeners=%d",
                     session_id, event_type, state.agent_status.value if hasattr(state.agent_status, 'value') else state.agent_status,
                     len(self._listeners))
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

            # 捕获当前 event_type/event_data 快照，供 _flush 使用
            # 避免 _flush 在 TURN_END 之后运行时读到错误的 state.last_event_type
            state = self._sessions.get(session_id)
            _snap_type = state.last_event_type if state else "chunk"
            _snap_data = dict(state.last_event_data) if state else {}

            def _flush():
                with self._chunk_lock:
                    self._chunk_timers.pop(session_id, None)
                    pending = self._chunk_pending.pop(session_id, False)
                if pending:
                    # 使用捕获的快照，避免 TURN_END 已经修改了 state.last_event_type
                    self._notify_with(session_id, _snap_type, _snap_data)

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

    def _notify_with(self, session_id: str, event_type: str, event_data: dict) -> None:
        """使用指定的 event_type/event_data 通知监听器（避免从共享 state 读取竞态数据）。"""
        state = self._sessions.get(session_id)
        if state:
            log.info("[DEBUG] _notify_with: session=%s event_type=%s data_keys=%s listeners=%d",
                     session_id, event_type, list(event_data.keys()), len(self._listeners))
            for listener in list(self._listeners):
                try:
                    listener(session_id, event_type, event_data)
                except Exception as e:
                    log.error("会话监听器异常: %s", e)

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
            err_msg = str(e)
            # 工作区不存在是常见问题（DSH 版本兼容性），尝试无 workspace 重试
            if workspace and "workspace" in err_msg.lower() and "not found" in err_msg.lower():
                log.warning("工作区不被 DSH 识别，尝试无 workspace 重试: %s", e)
                try:
                    info = self.dsh.create_session(title=title, model=model,
                                                    workspace="", agent_preset=agent_preset)
                    state = SessionState(info=info)
                    self._sessions[info.id] = state
                    self.switch_to(info.id)
                    return state
                except Exception as e2:
                    log.warning("无 workspace 重试仍然失败，降级为本地草稿会话: %s", e2)
            else:
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
            # 轮询锚点：此后 history 里产生的新消息才属于本轮
            state._poll_anchor_ts = time.time()
            # 启动兜底轮询：防止 WebSocket 丢事件导致 UI 永不刷新
            self._mark_session_poll_start(self._current_session_id)
            try:
                self.dsh.send_message(self._current_session_id, content, attachments)
                self._last_send_ok = True
            except Exception as e:
                log.error("发送消息失败: %s", e)
                rpc_error = e
                state.agent_status = AgentStatus.ERROR
                self._mark_session_poll_end(self._current_session_id)

        # ---- 失败回滚：移除乐观添加的消息 ----
        if not self._last_send_ok:
            if 0 <= append_index < len(state.messages) and \
                    state.messages[append_index] is optimistic_msg:
                popped = state.messages.pop(append_index)
                state._msg_ts_index.discard(popped.timestamp)   # 同步索引回滚
            if state.agent_status == AgentStatus.RUNNING:
                state.agent_status = AgentStatus.IDLE
                self._mark_session_poll_end(self._current_session_id)
                if state.current_turn > 0:
                    state.current_turn -= 1
            # 将原始异常携带到状态上，供 UI 层读取提示
            state.last_rpc_error = str(rpc_error) if rpc_error else ""

        self._notify(self._current_session_id)

    def cancel_agent(self) -> None:
        """停止 Agent 执行。

        如果正在流式输出，立即固化当前缓冲区为最终消息，
        避免轮询和 WebSocket 重复输出导致同一个回答出现多次。
        """
        if not self._current_session_id:
            return
        try:
            self.dsh.cancel_session(self._current_session_id)
            state = self.current_session
            if not state:
                return

            state.agent_status = AgentStatus.IDLE
            state.last_event_type = "turn_end"

            # 如果正在流式输出且有内容，立即固化为最终消息
            # （DSH 端已生成完整回答，避免轮询和 WebSocket 重复添加）
            if state.streaming_buffer:
                finalized_msg = MessageRecord(
                    role="assistant",
                    content=state.streaming_buffer,
                    timestamp=time.time(),
                )
                state.messages.append(finalized_msg)
                state._msg_ts_index.add(finalized_msg.timestamp)
                state.last_event_data = {"message": finalized_msg}
                state.streaming_buffer = ""
                log.info("停止 Agent：流式缓冲区已固化为一条消息 (%d 字符)", len(finalized_msg.content))
            else:
                state.last_event_data = {}

            # 停止轮询（不再拉取历史，避免重复添加）
            self._mark_session_poll_end(self._current_session_id)

            # 清理 chunk 定时器
            self._cancel_chunk_timer(self._current_session_id)

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
        """处理当前会话的事件流。

        注意：必须用 msg.session_id 取 state，不能用 self.current_session，
        否则兜底轮询把 A 会话的 finalized 写入时，若 B 会话是当前选中，
        B 会话的 UI 会显示 A 的回复，造成"第二对话返回第一回答"现象。
        """
        state = self._get_state(msg.session_id)
        if not state:
            return

        if msg.event_type == WSEventType.CHUNK:
            # 取消后防护：如果 Agent 已被用户停止（IDLE），忽略残留的 CHUNK 事件
            if state.agent_status == AgentStatus.IDLE:
                return

            # 流式文本块：text-delta → 正文缓冲；reasoning-delta → 思考缓冲（ThinkRow）
            # msg.data 结构: {'turn': N, 'step': N, 'chunk': {'type': 'text-delta'|'reasoning-delta'|..., 'text': '...'}}
            # 只从 text-delta / reasoning-delta 提取（block-end 含完整文本，会与 delta 重复）
            chunk_text = ""
            reasoning_text = ""
            chunk_data = msg.data.get("chunk", {})
            if isinstance(chunk_data, dict):
                ctype = chunk_data.get("type")
                raw = chunk_data.get("text", "")
                if ctype == "text-delta":
                    chunk_text = str(raw) if raw else ""
                elif ctype == "reasoning-delta":
                    reasoning_text = str(raw) if raw else ""
            state.streaming_buffer += chunk_text
            if reasoning_text:
                state.pending_reasoning += reasoning_text
            state.agent_status = AgentStatus.THINKING
            if chunk_text:
                state.last_event_type = "chunk"
                state.last_event_data = {"chunk": chunk_text}
                # 优化：CHUNK 节流，30ms 合并一次 UI 刷新（避免每个小 chunk 都重绘）
                self._notify_chunk(msg.session_id)
            if reasoning_text:
                # 思考增量单独通知（UI 更新 ThinkRow 实时摘要）
                state.last_event_type = "reasoning"
                state.last_event_data = {"reasoning": reasoning_text}
                self._notify(msg.session_id)

        elif msg.event_type == WSEventType.TURN_START:
            # 取消后防护：如果 Agent 已被用户停止，忽略新的 TURN_START
            if state.agent_status == AgentStatus.IDLE:
                return
            state.agent_status = AgentStatus.RUNNING
            state.current_turn = msg.data.get("turn", state.current_turn + 1)
            state.streaming_buffer = ""
            state.pending_reasoning = ""
            # 轮询锚点：新一轮开始，此后产生的消息才属于本轮
            state._poll_anchor_ts = time.time()
            state.last_event_type = "turn_start"
            state.last_event_data = {}
            # 启动兜底轮询（防止 WebSocket 后续 chunk/turn_end 全丢）
            self._mark_session_poll_start(msg.session_id)
            self._cancel_chunk_timer(msg.session_id)   # 清理旧定时器
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.TURN_END or msg.event_type == WSEventType.DONE:
            # 取消后防护：如果 Agent 已被用户停止且流式缓冲区已清空，忽略重复的 TURN_END
            if state.agent_status == AgentStatus.IDLE and not state.streaming_buffer:
                return

            # 一轮对话结束：先 flush 掉 CHUNK 节流，确保最后一块立即送达
            self._cancel_chunk_timer(msg.session_id)
            # 停止兜底轮询
            self._mark_session_poll_end(msg.session_id)
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
            turn_cost = 0.0
            if snap:
                try:
                    if hasattr(self.dsh, "usage") and self.dsh.usage is not None:
                        # model 兜底链：TOKEN_USAGE 事件 → 会话当前模型 → DSH 默认模型
                        model = (
                            snap.get("model")
                            or getattr(state.info, "model", "")
                            or getattr(self.dsh, "default_model", "")
                            or ""
                        )
                        provider = snap.get("provider") or state.pending_provider or ""
                        purpose = snap.get("purpose") or state.pending_purpose or ""
                        # 记录时间：优先取事件 msg.timestamp（秒级）转 ms，否则当前
                        ts_ms = int((msg.timestamp or time.time()) * 1000)
                        rec = self.dsh.usage.add_record(
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
                        # 计算本轮成本（auto 计价模式，与插件一致）
                        turn_cost = self.dsh.usage.cost_of(rec, regime="auto")
                except Exception as e:
                    log.warning("写入用量记录失败: %s", e)
                # 消费后清空快照
                state.pending_token_usage = {}
                state.pending_purpose = ""
                state.pending_provider = ""
            # 更新会话级成本统计（用于余额内联小部件）
            state.last_turn_cost = turn_cost
            state.session_total_cost += turn_cost
            state.agent_status = AgentStatus.IDLE
            state.last_event_type = "turn_end"
            # 携带本轮思考内容（UI 固化 ThinkRow 用），随后清空
            turn_reasoning = state.pending_reasoning
            state.pending_reasoning = ""
            state.last_event_data = {
                "message": finalized_msg,
                "reasoning": turn_reasoning,
                "todos": list(state.pending_todos),
                "approval": state.pending_approval,
            }

            # 文件变更追踪：TURN_END 后自动检测本轮文件变化（仅当有基线时）
            self._auto_scan_file_changes_after_turn(msg.session_id)

            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.TOOL_CALL:
            tool_name = msg.data.get("tool", msg.data.get("name", "unknown"))
            params = msg.data.get("params") or msg.data.get("arguments", {})
            call_id = msg.data.get("callId") or msg.data.get("call_id") or msg.data.get("id") or ""
            record = ToolCallRecord(
                tool_name=tool_name,
                params=params,
            )
            if call_id:
                record.extra["callId"] = call_id
            state.tool_calls.append(record)
            state.agent_status = AgentStatus.TOOL_EXECUTING
            state.last_event_type = "tool_call"
            state.last_event_data = {"tool_name": tool_name, "params": params}
            self._notify(msg.session_id)

            # ---- 客户端本地工具拦截执行 ----
            self._maybe_run_local_tool(msg.session_id, tool_name, params, call_id, record)

        else:
            # 前面的大 if/elif 链没有匹配：处理 TOOL_RESULT / STEP_START / STEP_END /
            # TOKEN_USAGE / ERROR 等事件。拆独立方法是为了避免与"客户端本地工具拦截"
            # 的多个方法体混排，引起缩进/作用域错乱。
            self._route_remaining_ws_events(msg)

    # =========================================================================
    #  客户端本地工具拦截（外置视觉、文件追踪等工具在客户端侧执行）
    # =========================================================================

    def _maybe_run_local_tool(self, session_id: str, tool_name: str,
                              params: dict, call_id: str,
                              record: ToolCallRecord) -> None:
        """判断工具是否为客户端本地工具，是则异步执行并回写结果。

        本地工具完成后的回传策略：
          1. 优先尝试 Typert: session.submitToolResult / tools.submit（若 DSH 支持）
          2. 不支持时回退到 session.updateQueue，把工具结果以规范化提示追加到对话队列，
             让 Agent 下一轮能读取结果继续推理
          3. 无论是否成功回传 RPC，都会写入 ToolCallRecord 并通过 TOOL_RESULT 事件通知 UI
        """
        # 目前仅支持 inspect_image（外置视觉）
        local_tools = {"inspect_image"}
        if tool_name not in local_tools:
            return

        if tool_name == "inspect_image":
            self._run_inspect_image(session_id, params, call_id, record)

    def _run_inspect_image(self, session_id: str, params: dict,
                           call_id: str, record: ToolCallRecord) -> None:
        """异步执行 inspect_image 本地工具。"""
        # 懒加载，避免 import 循环
        from ..tools.inspect_image import InspectImageResult, inspect_image_async

        image = params.get("image") or params.get("path") or params.get("url") or ""
        prompt = params.get("prompt") or params.get("question") or ""
        detail = params.get("detail") or "auto"

        if not image:
            self._finish_local_tool(
                session_id, "inspect_image", call_id, record,
                ok=False, error="参数缺失：需要 image（本地路径或 URL）",
                result_summary=None,
            )
            return

        def on_done(r: InspectImageResult) -> None:
            self._finish_local_tool(
                session_id, "inspect_image", call_id, record,
                ok=r.ok,
                error=r.error,
                result_summary=r.summary,
                extra={
                    "description": r.description,
                    "image_source": r.image_source,
                    "model": r.model,
                    "elapsed_ms": r.elapsed_ms,
                    "tokens_in": r.tokens_in,
                    "tokens_out": r.tokens_out,
                },
            )

        try:
            inspect_image_async(on_done, image=image, prompt=prompt, detail=detail)
        except Exception as e:
            log.error("启动 inspect_image 失败: %s", e)
            self._finish_local_tool(
                session_id, "inspect_image", call_id, record,
                ok=False, error=f"启动本地工具失败: {e}", result_summary=None,
            )

    def _finish_local_tool(self, session_id: str, tool_name: str, call_id: str,
                           record: ToolCallRecord, *,
                           ok: bool, error: str = "",
                           result_summary: str | None = None,
                           extra: dict | None = None) -> None:
        """本地工具完成后：写状态、通知 UI、并把结果回传给 DSH。"""
        state = self._get_state(session_id)
        if state is None:
            return

        status_txt = "success" if ok else "error"
        # 1) 更新 ToolCallRecord
        try:
            record.status = status_txt
            record.error = error or ""
            if result_summary is not None:
                record.result = result_summary
            record.finished_at = time.time()
            if extra:
                record.extra.update(extra)
        except Exception as e:
            log.warning("更新 ToolCallRecord 失败: %s", e)

        # 2) 构造 TOOL_RESULT 事件通知 UI（使工具卡片刷新）
        state.last_event_type = "tool_result"
        state.last_event_data = {
            "tool_name": tool_name,
            "status": status_txt,
            "result": record.result,
            "error": error or "",
            "callId": call_id,
            **(extra or {}),
        }
        self._notify(session_id)

        # 3) 回传结果给 DSH（让 Agent 能继续推理）
        self._submit_tool_result_to_dsh(
            session_id, tool_name, call_id,
            ok=ok, error=error, result=result_summary,
        )

    def _submit_tool_result_to_dsh(self, session_id: str, tool_name: str,
                                   call_id: str, *, ok: bool, error: str,
                                   result: str | None) -> None:
        """把本地工具结果回传给 DSH。

        优先使用 Typert RPC 的 tools.submit / session.submitToolResult；
        方法不存在时回退到 session.updateQueue（把结果当额外消息追加）。
        """
        if self.dsh is None or self.dsh.http is None:
            return

        normalized_result = result if result is not None else (
            f"[本地工具 {tool_name} 执行{'成功' if ok else '失败'}]"
            + (f" 原因: {error}" if error else "")
        )

        # 尝试 1: tools.submit（可能是 DSH Typert 原生工具回传方法）
        tried_methods: list[tuple[str, dict]] = [
            ("tools.submit", {
                "sessionId": session_id,
                "tool": tool_name,
                "callId": call_id,
                "status": "success" if ok else "error",
                "result": normalized_result,
                "error": error or "",
            }),
            # 尝试 2: session.submitToolResult
            ("session.submitToolResult", {
                "sessionId": session_id,
                "callId": call_id,
                "tool": tool_name,
                "status": "success" if ok else "error",
                "result": normalized_result,
                "error": error or "",
            }),
        ]

        last_exc: Exception | None = None
        for method_name, payload in tried_methods:
            try:
                self.dsh.http.call(method_name, payload)
                log.info("已通过 %s 回传本地工具结果: %s", method_name, tool_name)
                return
            except Exception as e:
                last_exc = e
                # method-not-found / 其他错误 → 继续尝试下一个
                continue

        # 回退：updateQueue → 把结果以规范化格式注入对话
        fallback_text = (
            f"\n\n[本地工具执行结果]\n工具: {tool_name}\n"
            f"状态: {'成功' if ok else '失败'}\n"
            + (f"错误: {error}\n" if error else "")
            + f"输出:\n{normalized_result}\n"
        )
        try:
            self.dsh.update_queue(session_id, fallback_text)
            log.info("已通过 updateQueue 回传本地工具结果: %s", tool_name)
        except Exception as e2:
            log.error(
                "本地工具结果回传 DSH 失败（RPC最后异常=%s, fallback=%s）",
                last_exc, e2,
            )

    def _handle_tool_result(self, msg: WSEventMessage) -> None:
        """处理 DSH 侧发回的 TOOL_RESULT 事件（服务端侧工具执行结果）。"""
        state = self._get_state(msg.session_id)
        if state is None:
            return
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

    # =====================================================================
    #  _on_ws_message() 剩余事件分支（接 TOOL_CALL 本地工具拦截之后）
    # =====================================================================

    def _route_remaining_ws_events(self, msg: WSEventMessage) -> None:
        """_on_ws_message 中 TOOL_CALL 之后的事件路由。

        单独拆方法是为了避免把本地工具拦截方法的函数体与大段 elif 链混排，
        导致缩进错乱。等价的 elif 链如下。
        """
        state = self._get_state(msg.session_id)
        if state is None:
            return

        if msg.event_type == WSEventType.TOOL_RESULT:
            self._handle_tool_result(msg)

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

        # ===== Web 版高级交互事件 =====

        elif msg.event_type == WSEventType.TODO_WRITE:
            # 计划更新（TodoDock 数据源）
            todos = msg.data.get("todos") if isinstance(msg.data.get("todos"), list) else []
            state.pending_todos = [t for t in todos if isinstance(t, dict)]
            state.last_event_type = "todo_write"
            state.last_event_data = {"todos": state.pending_todos}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.APPROVAL_POLICY:
            # 审批策略（ask / 其它）
            state.last_event_type = "approval_policy"
            state.last_event_data = dict(msg.data) if isinstance(msg.data, dict) else {}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.APPROVAL_ASKED:
            # 审批请求（ApprovalPanel 数据源）
            state.pending_approval = dict(msg.data) if isinstance(msg.data, dict) else {}
            state.agent_status = AgentStatus.THINKING
            state.last_event_type = "approval_asked"
            state.last_event_data = state.pending_approval
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.APPROVAL_DECIDED:
            # 审批结果（放行/拒绝）
            state.pending_approval = None
            state.last_event_type = "approval_decided"
            state.last_event_data = dict(msg.data) if isinstance(msg.data, dict) else {}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.QUEUE_SPLICED:
            # 队列/steer 变更（QueueDock 数据源）
            state.pending_queue.append(dict(msg.data) if isinstance(msg.data, dict) else {})
            state.last_event_type = "queue_spliced"
            state.last_event_data = dict(msg.data) if isinstance(msg.data, dict) else {}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.GOAL_CHANGE:
            state.last_event_type = "goal_change"
            state.last_event_data = dict(msg.data) if isinstance(msg.data, dict) else {}
            self._notify(msg.session_id)

        elif msg.event_type == WSEventType.USER_MESSAGE:
            # 用户/注入消息：source.kind 区分来源（user=本人；其它=上下文注入/召回）
            data = dict(msg.data) if isinstance(msg.data, dict) else {}
            source = data.get("source") if isinstance(data.get("source"), dict) else {}
            kind = str(source.get("kind") or "user")
            if kind == "user":
                # 普通用户消息：由消息流正常渲染（一般已通过 user/message 事件处理）
                return
            # 上下文注入行（Web 版 ContextRow）
            text = ""
            content = data.get("content")
            if isinstance(content, list):
                texts = []
                for b in content:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        texts.append(b["text"])
                text = "\n".join(texts)
            label = {
                "injection": "上下文注入",
                "recall": "跨会话召回",
                "skill": "技能加载",
                "workspace": "工作区上下文",
            }.get(kind, f"上下文（{kind}）")
            state.last_event_type = "context_row"
            state.last_event_data = {"label": label, "content": text}
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

    # =====================================================================
    #  文件变更追踪 + 一键还原（外接 UI 面板 / 工具卡）
    # =====================================================================

    def _get_file_tracker(self, session_id: str | None = None,
                          create: bool = True) -> Any:  # FileChangeTracker | None
        """获取指定会话的文件追踪器。create=True 时懒加载。"""
        sid = session_id or self._current_session_id
        if not sid:
            return None
        state = self._get_state(sid)
        if state is None:
            return None
        if state._file_tracker is None and create:
            from .file_tracker import FileChangeTracker
            tracker = FileChangeTracker(session_id=sid)
            tracker.load_persistent()  # 崩溃重启恢复
            state._file_tracker = tracker
        return state._file_tracker

    def snapshot_files(self, paths, session_id: str | None = None) -> int:
        """为指定路径建立基线快照。返回新建立的快照文件数。"""
        tracker = self._get_file_tracker(session_id, create=True)
        if tracker is None:
            return 0
        n = tracker.snapshot(paths)
        # 建立基线后立即扫描一次，便于 UI 渲染列表
        tracker.scan_changed()
        self._notify_any(session_id)
        return n

    def scan_file_changes(self, session_id: str | None = None):
        """返回 list[ChangedFile]（按路径排序）。"""
        tracker = self._get_file_tracker(session_id, create=False)
        if tracker is None:
            return []
        return tracker.scan_changed()

    def get_file_changes(self, session_id: str | None = None):
        """UI 刷新用：返回上次 scan 结果（不重新扫描磁盘）。"""
        tracker = self._get_file_tracker(session_id, create=False)
        if tracker is None:
            return []
        return tracker.get_last_changes()

    def diff_file(self, path: str, session_id: str | None = None) -> str:
        """返回单文件统一 diff（基线 vs 当前）。"""
        tracker = self._get_file_tracker(session_id, create=False)
        if tracker is None:
            return ""
        return tracker.diff_lines(path)

    def restore_file(self, path: str, force: bool = False,
                     session_id: str | None = None) -> tuple[bool, str]:
        """还原单文件。返回 (是否成功, 信息/错误文本)。"""
        tracker = self._get_file_tracker(session_id, create=False)
        if tracker is None:
            return False, "无基线快照（请先对该文件 snapshot_files）。"
        from .file_tracker import ConflictError
        try:
            tracker.restore_one(path, force=force)
        except ConflictError as e:
            return False, str(e)
        except Exception as e:
            return False, f"还原失败: {e}"
        self._notify_any(session_id)
        return True, "已还原。"

    def restore_all_files(self, force: bool = False,
                          session_id: str | None = None
                          ) -> tuple[int, list[tuple[str, str]]]:
        """一键还原全部扫描到的变更。返回 (成功数, [(path, err), ...])。"""
        tracker = self._get_file_tracker(session_id, create=False)
        if tracker is None:
            return 0, [("", "无基线快照。")]
        ok, errors = tracker.restore_all(force=force)
        self._notify_any(session_id)
        return ok, errors

    def add_file_change_listener(self, cb, session_id: str | None = None) -> None:
        """注册文件变更监听器（scan_changed 检测到变更时触发）。"""
        tracker = self._get_file_tracker(session_id, create=True)
        if tracker is not None:
            tracker.add_listener(cb)

    def clear_file_tracker(self, session_id: str | None = None) -> None:
        tracker = self._get_file_tracker(session_id, create=False)
        if tracker is not None:
            tracker.clear()

    def _notify_any(self, session_id: str | None) -> None:
        sid = session_id or self._current_session_id
        if sid:
            self._notify(sid)

    def _auto_scan_file_changes_after_turn(self, session_id: str) -> None:
        """TURN_END 后：如果本会话已做过基线快照，就自动做一次变更扫描。

        扫描到变更时通过 file_tracker listener 广播，UI 侧面板能即时刷新列表。
        """
        tracker = self._get_file_tracker(session_id, create=False)
        if tracker is None:
            return
        try:
            changes = tracker.scan_changed()
            if changes:
                log.info("本轮检测到 %d 个文件变更（session=%s）", len(changes), session_id)
        except Exception as e:
            log.warning("TURN_END 自动扫描文件变更失败: %s", e)
