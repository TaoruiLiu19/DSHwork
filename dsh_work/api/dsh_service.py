"""DSH 服务门面：统一暴露 DSH 通信层能力（适配 Typert 0.1.0-rc.6 协议）。

向上层（业务逻辑层）提供纯 Python 接口，屏蔽 HTTP/WebSocket 细节。
业务层只依赖此门面，不直接接触 HttpClient / WebSocketClient / VersionAdapter。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .. import constants as C
from ..utils.logger import get_logger

# from ..core.usage_tracker import UsageTracker  # 懒加载：避免循环导入
from .balance_client import BalanceClient, BalanceResult
from .http_client import HttpClient, RpcError
from .reconnect import ReconnectManager
from .version_adapter import AdapterProbeResult, CompatibilityMode, VersionAdapter
from .ws_client import WebSocketClient

log = get_logger("api.dsh_service")


def _get(data: dict, *keys: str, default: Any = None) -> Any:
    """从 dict 里按优先级取多个可能的键（兼容 camelCase / snake_case）。"""
    for k in keys:
        if k in data and data[k] is not None:
            return data[k]
    return default


@dataclass
class SessionInfo:
    """会话信息。

    除基础字段外，携带 session.list 的 projections（Web 版投影数据）：
    - sessionStats: {turns, steps, llmMs, toolMs, ttftMs, decodeMs, decodeTokens}
    - tokenUsage / contextPressure / contextBreakdown
    - todos: [{content, status}]  ← TodoDock 计划条数据源
    - goal / plan / permissions / subagent / imageLimits
    """

    id: str
    title: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    model: str = ""
    message_count: int = 0
    cwd: str = ""
    running: bool = False
    blank: bool = False
    projections: dict = field(default_factory=dict)


@dataclass
class ModelInfo:
    """模型信息。"""

    id: str
    name: str = ""
    context_length: int = 0
    description: str = ""


@dataclass
class MessageRecord:
    """历史消息记录。"""

    role: str  # user / assistant / tool / system
    content: str
    timestamp: float = 0.0
    tool_calls: list[dict] | None = None
    metadata: dict[str, Any] | None = None


class DshService:
    """DSH 通信层门面。

    聚合 HttpClient、WebSocketClient、VersionAdapter、BalanceClient、ReconnectManager，
    向业务层提供统一接口。
    """

    def __init__(self, base_url: str | None = None):
        self.http = HttpClient(base_url=base_url)
        ws_base = None
        if base_url:
            ws_base = base_url.replace("http://", "ws://").replace("https://", "wss://")
        self.ws = WebSocketClient(ws_base_url=ws_base)
        self.adapter = VersionAdapter(self.http)
        self.balance = BalanceClient(self.http)
        # 降级通道预注入：DSH 不支持 credentials.getBalance 时，从本机凭据文件读取
        # 临时 Key（仅内存，不落盘），保证余额查询可直连平台降级。
        self._inject_balance_temp_key()
        # 懒加载：避免 api → core → session_manager → api 的循环导入
        from ..core.usage_tracker import UsageTracker
        self.usage = UsageTracker()  # 用量与消耗追踪（10 万条持久化 + 峰谷计费）
        self.reconnect = ReconnectManager()

        self._ws_started = False
        # session.models 需要 sessionId，get_models 首次调用时懒创建临时 session 复用
        self._bootstrap_session_id: str | None = None
        # bootstrap 临时会话 id 集合：这些会话不展示给用户（Web 侧创建的会话不受影响）
        self._bootstrap_session_ids: set[str] = set()
        # model -> provider 缓存（从 session.models.groups 提取，给 selectModel 用）
        self._model_provider: dict[str, str] = {}
        self._default_provider: str = "deepseek-official"  # DSH 0.0.1 默认内置 provider id
        # DSH 当前默认模型（host.describe 探测，用量记录 model 兜底用）
        self.default_model: str = ""

    # ===== 生命周期 =====

    def _inject_balance_temp_key(self) -> None:
        """从本机凭据文件读取 API Key，注入余额降级通道（仅内存，不落盘）。

        首选通道 credentials.getBalance 依赖 DSH 支持该 RPC；若 DSH 版本不支持，
        降级通道需要平台直连，此时必须有 Key。这里统一从 ~/.dsh/.credentials.yaml
        读取，避免调用方各自实现读取逻辑。
        """
        try:
            import os
            import re
            creds_file = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")
            if not os.path.isfile(creds_file):
                return
            with open(creds_file, encoding="utf-8") as f:
                txt = f.read()
            m = re.search(r"DEEPSEEK_API_KEY\s*:\s*['\"]?([^'\"\n]+)", txt)
            if m and m.group(1).strip():
                self.balance.set_temp_api_key(m.group(1).strip())
        except Exception as e:
            log.warning("注入余额临时 Key 失败（不致命）: %s", e)

    def initialize(self) -> AdapterProbeResult:
        """初始化：探测 DSH 版本与能力。"""
        result = self.adapter.probe()
        if result.mode == CompatibilityMode.FULL:
            log.info("DSH 服务初始化完成: version=%s mode=FULL", result.version)
            # 探测当前默认模型（用量记录 model 兜底：TOKEN_USAGE 无 model 字段时用）
            try:
                desc = self.http.call("host.describe", {})
                if isinstance(desc, dict) and desc.get("model"):
                    self.default_model = str(desc["model"])
            except Exception:
                pass
        elif result.mode == CompatibilityMode.DEGRADED:
            log.warning("DSH 版本适配器探测失败: %s，进入%s模式",
                        result.error, result.mode.value)
        else:
            log.warning("DSH 离线: %s", result.error)
        return result

    def start_websocket(self) -> None:
        """启动 WebSocket 客户端。"""
        if self._ws_started:
            return
        self.ws.start()
        self._ws_started = True

    def shutdown(self) -> None:
        """关闭所有连接。"""
        self.ws.stop()
        self.balance.close()
        self.http.close()
        self._ws_started = False
        log.info("DSH 服务已关闭")
    # ===== 适配器信息 =====

    @property
    def compatibility_mode(self) -> CompatibilityMode:
        return self.adapter.mode

    @property
    def dsh_version(self) -> str:
        return self.adapter.version

    @property
    def is_full_mode(self) -> bool:
        return self.adapter.mode == CompatibilityMode.FULL

    @property
    def is_offline(self) -> bool:
        return self.adapter.mode == CompatibilityMode.OFFLINE

    def has_capability(self, name: str) -> bool:
        return self.adapter.has_capability(name)

    # ===== 会话 RPC =====

    def create_session(self, title: str = "", model: str = "", workspace: str = "",
                       agent_preset: str = "") -> SessionInfo:
        """创建新会话。

        DSH Typert schema 严格 camelCase，缺字段用默认值：
          payload 允许完全为空（{}），DSH 会生成默认会话。
          title / model / workspaceId / agentPreset 仅在非空时传，避免 unknown-field 校验。

        工作区路径容错：
          · 若路径包含非 ASCII 字符（如中文），DSH 可能无法识别
          · 若路径不存在于磁盘上，DSH 同样无法识别
          以上两种情况跳过 workspace 参数，避免 session.create 返回 workspace-not-found 错误。
        """
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if model:
            payload["model"] = model
            provider = self._model_provider.get(model) or self._default_provider
            payload["provider"] = provider

        # 工作区路径容错：非 ASCII 字符或路径不存在时跳过 workspace 参数
        _workspace_ok = False
        if workspace:
            try:
                # 检查是否包含非 ASCII 字符
                if not all(ord(c) < 128 for c in workspace):
                    log.warning("工作区路径包含非 ASCII 字符，DSH 可能无法识别，跳过 workspace 参数: %s", workspace)
                elif not os.path.isdir(workspace):
                    log.warning("工作区路径不存在，跳过 workspace 参数: %s", workspace)
                else:
                    _workspace_ok = True
            except Exception:
                pass
        if _workspace_ok:
            payload["workspaceId"] = workspace

        if agent_preset:
            # sessionCreateRequestSchema 显式定义了 agentPreset: z.string().optional()
            payload["agentPreset"] = agent_preset
        try:
            result = self.http.call("session.create", payload)
        except RpcError:
            # 若字段导致 schema 拒绝，回退裸 payload（保证会话至少能创建成功）
            result = self.http.call("session.create", {})
        info = self._parse_session(result)
        # 若还没有 bootstrap session，把首次创建的 sessionId 存下来（get_models 要用）
        if not self._bootstrap_session_id and info.id:
            self._bootstrap_session_id = info.id
        return info

    def list_sessions(self) -> list[SessionInfo]:
        """列出所有会话。

        只过滤本客户端为拉取模型列表而创建的 bootstrap 临时会话
        （其 sessionId 记录在 _bootstrap_session_ids 中）；
        Web 版 / 其他客户端创建的空白会话会正常展示（互通）。
        """
        result = self.http.call("session.list", {})
        items: Any = []
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = (
                result.get("items")
                or result.get("sessions")
                or result.get("data")
                or []
            )
        if not isinstance(items, list):
            return []
        all_infos = [self._parse_session(s) for s in items if isinstance(s, dict)]
        # 缓存第一个可用 sessionId 作为 get_models 的兜底（从全量列表中取）
        if not self._bootstrap_session_id and all_infos:
            self._bootstrap_session_id = all_infos[0].id
        # 只过滤 bootstrap 临时会话（内部 id 集合命中），其余全部展示
        infos = [s for s in all_infos if s.id not in self._bootstrap_session_ids]
        return infos

    def get_history(
        self,
        session_id: str,
        since: str | None = None,
        since_timestamp: float | None = None,
        limit: int = 50,
    ) -> list[MessageRecord]:
        """获取会话历史消息（支持增量恢复）。

        DSH schema 严格要求：必须 sessionId: string，其他字段可选；
        未知字段（如 session_id）不会报错，但传了也无效，这里尽量干净只传 camelCase 正名。
        """
        params: dict[str, Any] = {
            "sessionId": session_id,
            "limit": limit,
        }
        if since:
            params["beforeSeq"] = since
        if since_timestamp:
            params["sinceTimestamp"] = since_timestamp
        result = self.http.call("session.history", params)
        # DSH 返回 {events, hasMore, projections}；events 里每条是
        # {"event": {"type": "user/message|assistant/message|...", "data": {...}}}。
        # 只提取消息类事件，避免把 sandbox/mode、turn/start 等非消息事件当成消息。
        messages: Any = []
        if isinstance(result, dict):
            events = result.get("events")
            if isinstance(events, list):
                messages = self._extract_messages_from_events(events)
            else:
                for key in ("messages", "items", "entries"):
                    arr = result.get(key)
                    if isinstance(arr, list):
                        messages = arr
                        break
        elif isinstance(result, list):
            messages = result
        if not isinstance(messages, list):
            return []
        return [self._parse_message(m) for m in messages if isinstance(m, dict)]

    @staticmethod
    def _extract_messages_from_events(events: list) -> list[dict]:
        """从 session.history 的 events 里筛出消息类事件（user/assistant/tool/system）。

        每个 event 形如 {"event": {"type": "...", "data": {...}}}，也可能直接平铺。
        只返回消息事件携带的 data（非消息事件如 sandbox/mode、turn/start 会被忽略，
        它们没有 role/content，不应出现在消息记录里）。
        """
        _MESSAGE_EVENT_TYPES = {
            "user/message", "assistant/message", "tool/message", "system/message",
            "user_message", "assistant_message", "tool_message", "system_message",
        }
        out: list[dict] = []
        for e in events:
            if not isinstance(e, dict):
                continue
            ev = e.get("event") if isinstance(e.get("event"), dict) else e
            etype = str(ev.get("type") or "")
            data = ev.get("data")
            if not isinstance(data, dict):
                continue
            if etype in _MESSAGE_EVENT_TYPES:
                out.append(data)
            elif "role" in data or "message" in data:
                # 兼容非标准结构：直接带 role 或 message 子对象的视为消息
                out.append(data)
        return out

    def get_full_events(self, session_id: str, limit: int = 200) -> list[dict]:
        """获取会话完整事件流（与 Web 版同源：含 reasoning-delta、approval、todo 等）。

        session.history 返回 {events:[{event:{type,seq,time,data}},...]}，
        这里解包为 [{type, seq, time, data}] 平铺结构，供 Think 行 / Context 行 /
        审批 / 计划的历史回放。limit 为事件条数上限。
        """
        params: dict[str, Any] = {"sessionId": session_id, "limit": limit}
        try:
            result = self.http.call("session.history", params)
        except RpcError:
            return []
        events_raw: Any = []
        if isinstance(result, dict):
            events_raw = result.get("events") or []
        elif isinstance(result, list):
            events_raw = result
        out: list[dict] = []
        for e in events_raw:
            if not isinstance(e, dict):
                continue
            ev = e.get("event") if isinstance(e.get("event"), dict) else e
            etype = str(ev.get("type") or "")
            if not etype:
                continue
            out.append({
                "type": etype,
                "seq": ev.get("seq", 0),
                "time": ev.get("time", 0),
                "data": ev.get("data") if isinstance(ev.get("data"), dict) else {},
            })
        out.sort(key=lambda x: x.get("seq", 0))
        return out

    def select_model(self, session_id: str, model: str) -> bool:
        """切换模型。DSH Typert 严格要求 {sessionId, model, provider} 三个 camelCase 字段。"""
        provider = self._model_provider.get(model)
        if not provider:
            # 缓存未命中 → 先通过 get_models 拉一次（也会回填缓存）
            self.get_models()
            provider = self._model_provider.get(model) or self._default_provider
        self.http.call(
            "session.selectModel",
            {"sessionId": session_id, "model": model, "provider": provider},
        )
        return True

    def get_models(self) -> list[ModelInfo]:
        """获取可用模型列表。需要 sessionId，内部懒创建 bootstrap session。"""
        sid = self._bootstrap_session_id
        if not sid:
            # 先尝试从 list_sessions 捡一个现成的（顺便设置 _bootstrap_session_id）
            try:
                self.list_sessions()
                sid = self._bootstrap_session_id
            except Exception:
                pass
        if not sid:
            try:
                # 创建临时 bootstrap session（不传任何参数 → schema 最稳）
                tmp = self.http.call("session.create", {})
                info = self._parse_session(tmp)
                sid = info.id
                if sid:
                    self._bootstrap_session_id = sid
                    # 记录为内部临时会话，list_sessions 时不展示
                    self._bootstrap_session_ids.add(sid)
            except Exception as e:
                log.debug("get_models 创建 bootstrap session 失败: %s", e)
                return []
        if not sid:
            return []
        try:
            result = self.http.call("session.models", {"sessionId": sid})
        except RpcError as e:
            log.debug("session.models 调用失败(通常未配置或无权限): %s", e)
            return []

        # 解析结构：{current:{provider,model}, routable:bool, groups:[{id,name,models:[{id,name,reasoning:{...},...}],...}], failures:[]}
        items: list[dict] = []
        if isinstance(result, list):
            items = [m for m in result if isinstance(m, dict)]
        elif isinstance(result, dict):
            # 回填默认 provider
            cur = result.get("current")
            if isinstance(cur, dict) and cur.get("provider"):
                self._default_provider = str(cur["provider"])
            # groups[].models[] 展开
            groups = result.get("groups") or []
            if isinstance(groups, list):
                for g in groups:
                    if not isinstance(g, dict):
                        continue
                    g_id = str(g.get("id") or self._default_provider)
                    models = g.get("models") or []
                    if isinstance(models, list):
                        for m in models:
                            if isinstance(m, dict):
                                # 缓存 model -> provider 映射
                                mid = m.get("id")
                                if mid:
                                    self._model_provider[str(mid)] = g_id
                                items.append(m)
            # fallback: routable 不是数组（布尔），不用管；failures 忽略
            if not items:
                for key in ("models", "items", "data"):
                    arr = result.get(key)
                    if isinstance(arr, list):
                        items.extend(m for m in arr if isinstance(m, dict))
                        break
        return [self._parse_model(m) for m in items]

    def get_agent_presets(self) -> list[dict]:
        """读取可用 Agent 预设（agentPreset.list 不需要参数，返回 {presets, authorable, hasDocument}）。"""
        try:
            result = self.http.call("agentPreset.list", {})
        except RpcError:
            return []
        items: Any = []
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict):
            items = (
                result.get("presets")
                or result.get("items")
                or result.get("data")
                or []
            )
        return list(items) if isinstance(items, list) else []

    _IMAGE_MEDIA_TYPES = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }

    def send_message(self, session_id: str, content: str, attachments: list[str] | None = None) -> Any:
        """发送消息到会话（触发 Agent 流式生成，事件走 events.mux WS）。

        当前 DSH 运行时(0.0.1)的 session.prompt 采用严格 schema：
          payload = { sessionId, mode: "queue"|"steer", content: [text|image 块] }
          - 空闲发消息用 mode="steer"
          - content 为块数组：{"type":"text","text":str} 或
            {"type":"image","mediaType":"image/png|jpeg|webp|gif","data":<base64>}
        旧版直接传字符串 content 的写法会被 schema 拒绝（bad-request）。
        """
        blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
        for att in (attachments or []):
            block = self._image_content_block(att)
            if block:
                blocks.append(block)
            else:
                log.warning("附件非受支持图片，发送消息时忽略: %s", att)
        payload: dict[str, Any] = {
            "sessionId": session_id,
            "mode": "steer",
            "content": blocks,
        }
        # Typert: session.prompt 返回的是 {accepted:true} 之类的回执（流式 chunk 走 WS）
        return self.http.call("session.prompt", payload)

    @staticmethod
    def _image_content_block(path: str) -> dict[str, Any] | None:
        """把本地图片路径转成 session.prompt 的 image 内容块；非图片返回 None。"""
        try:
            import base64
            ext = os.path.splitext(path)[1].lower()
            media_type = DshService._IMAGE_MEDIA_TYPES.get(ext)
            if not media_type:
                return None
            with open(path, "rb") as f:
                data = f.read()
            return {
                "type": "image",
                "mediaType": media_type,
                "data": base64.b64encode(data).decode("ascii"),
            }
        except Exception as e:
            log.warning("构造图片内容块失败 %s: %s", path, e)
            return None

    def update_queue(self, session_id: str, content: str) -> Any:
        """Agent 运行时追加消息到队列（排队等待下一轮）。"""
        return self.http.call(
            "session.updateQueue",
            {"sessionId": session_id, "content": content},
        )

    def cancel_session(self, session_id: str) -> Any:
        """取消当前 Agent 执行。"""
        return self.http.call(
            "session.cancel",
            {"sessionId": session_id},
        )

    def delete_session(self, session_id: str) -> bool:
        """删除会话。DSH Typert 方法名 session.delete（若不存在会返回 method-not-found）。"""
        try:
            self.http.call("session.delete", {"sessionId": session_id})
            return True
        except RpcError as e:
            # 兼容旧版 DSH：方法名可能是 session.remove
            if e.code == "method-not-found":
                try:
                    self.http.call("session.remove", {"sessionId": session_id})
                    return True
                except RpcError:
                    return False
            log.warning("删除会话失败 session=%s: %s", session_id, e)
            return False

    def rename_session(self, session_id: str, title: str) -> bool:
        """重命名会话。DSH Typert 方法名 session.update（payload: sessionId + title）。"""
        try:
            self.http.call("session.update", {"sessionId": session_id, "title": title})
            return True
        except RpcError as e:
            # 兼容：方法名可能是 session.rename
            if e.code == "method-not-found":
                try:
                    self.http.call("session.rename", {"sessionId": session_id, "title": title})
                    return True
                except RpcError:
                    return False
            log.warning("重命名会话失败 session=%s: %s", session_id, e)
            return False

    def check_credentials(self) -> dict:
        """检查 API Key 状态。返回规范化 dict：

        有配置时：{'has_key': True, 'configured': True, 'valid': bool, 'provider': str, 'details': {...}}
        无配置或接口错误：返回空 dict（旧代码对 Falsey 值当"未配置"处理）。
        """
        # DSH Typert schema 严格要求 refs: array。必须传具体的 ref 名才查得到，
        # 传空数组 = 什么都不查，永远返回空。LLM 适配器默认读 DEEPSEEK_API_KEY。
        ref = "DEEPSEEK_API_KEY"
        try:
            result = self.http.call("credentials.describe", {"refs": [ref]})
        except RpcError:
            # ok:false 表示未配置 / 读取失败（最常见：尚未 set API Key）
            return {}
        if not isinstance(result, dict):
            return {}

        # DSH 返回 {credentials: {DEEPSEEK_API_KEY: {configured, source, writable}}}，
        # 取该 ref 的视图。
        creds_map = result.get("credentials")
        if isinstance(creds_map, dict):
            view = creds_map.get(ref)
            if isinstance(view, dict):
                configured = bool(view.get("configured"))
                return {
                    "configured": configured,
                    "has_key": configured,
                    "hasKey": configured,
                    "valid": configured,
                    "provider": str(view.get("source") or ""),
                    "raw": result,  # 原始值保留给调试用
                }
            # 该 ref 未配置
            return {}

        # 兼容旧结构：直接剥一层
        inner = result.get("credentials") if isinstance(result.get("credentials"), dict) else result
        configured = bool(
            _get(inner, "configured", "hasKey", "has_key", "isConfigured", "set")
        )
        valid = bool(_get(inner, "valid", "isValid", "ok", default=configured))
        provider = str(_get(inner, "provider", "activeProvider", default="") or "")
        return {
            "configured": configured,
            "has_key": configured,
            "hasKey": configured,
            "valid": valid,
            "provider": provider,
            "raw": result,
        }

    # ===== 余额查询 =====

    def query_balance(self, force: bool = False) -> BalanceResult:
        return self.balance.query(force=force)

    # ===== WebSocket =====

    def add_ws_handler(self, handler) -> None:
        self.ws.add_handler(handler)

    def remove_ws_handler(self, handler) -> None:
        self.ws.remove_handler(handler)

    def switch_session(self, session_id: str | None) -> None:
        """切换当前活跃会话的 WebSocket 过滤。"""
        self.ws.switch_session(session_id)

    # ===== 解析辅助 ===== 同时接受 snake_case / camelCase，并适配 DSH 的返回值形状 =====

    @staticmethod
    def _parse_session(data: dict) -> SessionInfo:
        if not isinstance(data, dict):
            return SessionInfo(id="")
        # DSH 顶层可能直接返回 {sessionId, agentPreset}，也可能包裹在 .session 子对象
        inner = data
        if isinstance(data.get("session"), dict):
            inner = data["session"]
        sid = str(
            _get(inner, "id", "sessionId", "session_id", default="")
            or _get(data, "sessionId", default="")
            or ""
        )
        title = str(_get(inner, "title", default=""))
        model = str(_get(inner, "model", "modelId", "activeModel", default=""))
        created_at = float(_get(inner, "createdAt", "created_at", "created", default=0) or 0)
        updated_at = float(_get(inner, "updatedAt", "updated_at", "updated", default=0) or 0)
        message_count = int(_get(inner, "messageCount", "message_count", "size", "count", default=0) or 0)
        # —— Web 版投影数据（session.list 的 projections.values）——
        projections: dict = {}
        pj = inner.get("projections")
        if isinstance(pj, dict):
            vals = pj.get("values")
            if isinstance(vals, dict):
                projections = vals
        # 标题可能在 projections.values.title（list 返回结构）
        if not title and isinstance(projections.get("title"), str):
            title = projections["title"]
        cwd = str(_get(inner, "cwd", default=""))
        running = bool(_get(inner, "running", default=False))
        blank = bool(_get(inner, "blank", default=False))
        return SessionInfo(
            id=sid,
            title=title,
            created_at=_parse_iso_ts(created_at, raw=created_at),
            updated_at=_parse_iso_ts(updated_at, raw=updated_at),
            model=model,
            message_count=message_count,
            cwd=cwd,
            running=running,
            blank=blank,
            projections=projections,
        )

    @staticmethod
    def _parse_model(data: dict) -> ModelInfo:
        model_id = str(_get(data, "id", "modelId", "model", default=""))
        raw_len = _get(data, "contextLength", "context_length", "contextWindow", default=None)
        try:
            context_length = int(raw_len) if raw_len else 0
        except (TypeError, ValueError):
            context_length = 0
        if not context_length:
            context_length = int(
                C.MODEL_CONTEXT_LENGTH_FALLBACK.get(model_id, C.DEFAULT_CONTEXT_LENGTH)
            )
        return ModelInfo(
            id=model_id,
            name=str(_get(data, "name", "displayName", default=model_id or "")),
            context_length=context_length,
            description=str(_get(data, "description", "desc", default="")),
        )

    @staticmethod
    def _parse_message(data: dict) -> MessageRecord:
        if not isinstance(data, dict):
            return MessageRecord(role="user", content="")
        # DSH 的消息 data 有两种形态：
        #   user/message  : {role, content, source, id} —— 角色/内容在顶层
        #   assistant/message|tool/message : {message: {role, content, ...}, usage, ...}
        #     —— 角色/内容在 message 子对象里
        msg = data.get("message") if isinstance(data.get("message"), dict) else data
        role = str(_get(msg, "role", default="user") or "user")
        # content 可能是字符串，也可能是 [{type:"text",text:...}] 的块列表
        raw_content = _get(msg, "content", default="")
        if isinstance(raw_content, list):
            texts = []
            for block in raw_content:
                if isinstance(block, dict):
                    t = block.get("text") or block.get("content")
                    if isinstance(t, str):
                        texts.append(t)
            content = "\n".join(texts)
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            content = str(raw_content or "")
        ts_raw = _get(data, "timestamp", "createdAt", "created_at", "time", default=0)
        ts = _parse_iso_ts(ts_raw, raw=ts_raw)
        tool_calls = _get(msg, "toolCalls", "tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, list):
            tool_calls = None
        metadata = _get(msg, "metadata", "meta", default=None)
        if metadata is not None and not isinstance(metadata, dict):
            metadata = None
        return MessageRecord(
            role=role,
            content=content,
            timestamp=ts,
            tool_calls=tool_calls,
            metadata=metadata,
        )


def _parse_iso_ts(value: Any, raw: Any = None) -> float:
    """把 DSH 可能返回的 ISO 字符串（如 "2026-01-01T00:00:00Z"）转换为 Unix 时间戳。

    已经是数字就直接转 float；失败返回 raw 或 0。
    """
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime
            # 支持 2026-...T...Z 和带小数秒的格式
            s = value.replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            pass
    if isinstance(raw, (int, float)):
        return float(raw)
    return 0.0
