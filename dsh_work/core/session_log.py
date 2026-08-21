"""会话完整工作记录读取器（对齐 DSH 持久化层 session.jsonl.zstd）。

DSH 将每个会话的全部事件（消息/思考/工具/审批/计划/队列）持久化到
~/.dsh/sessions/<cwd-encoded>/<sessionId>/session.jsonl.zstd —— 这是桌面版与
Web 版共享的"权威工作记录"。本模块提供：

- find_session_log(session_id)  定位日志文件
- read_session_events(session_id)  读取完整原始事件列表（按 seq 排序）
- read_full_record(session_id)  聚合为结构化工作记录（turn/messages/reasoning/
  tool_calls/approvals/todos/queue_splices），供高级交互历史回放与互通展示

复用 session_watcher 的 zstd 帧扫描/解压与存储行展开逻辑，不重复实现。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_dsh_sessions_dir
from ..utils.logger import get_logger
from .session_watcher import _decode_frame, expand_row, scan_zstd_frames

log = get_logger("core.session_log")


@dataclass
class TurnRecord:
    """一轮对话的聚合记录。"""

    turn: int = 0
    started_at: float = 0.0
    ended_at: float = 0.0
    reason: str = ""
    user_messages: list[dict] = field(default_factory=list)
    assistant_messages: list[dict] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)   # 思考文本（块级拼接）
    tool_calls: list[dict] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)
    todos: list[dict] = field(default_factory=list)
    steps: int = 0


@dataclass
class FullRecord:
    """完整工作记录。"""

    session_id: str = ""
    title: str = ""
    cwd: str = ""
    agent_preset: str = ""
    created_at: float = 0.0
    events: list[dict] = field(default_factory=list)      # 原始事件（按 seq）
    turns: list[TurnRecord] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)   # 全量审批（含跨 turn）
    todos: list[dict] = field(default_factory=list)       # 全量 todo/write
    queue_splices: list[dict] = field(default_factory=list)  # agent/inbox/spliced
    goals: list[dict] = field(default_factory=list)


def _iter_session_log_dirs() -> list[Path]:
    """枚举 sessions 目录下所有包含 session.jsonl.zstd 的目录。"""
    base = Path(get_dsh_sessions_dir())
    out: list[Path] = []
    if not base.is_dir():
        return out
    try:
        for root, _dirs, files in os.walk(base):
            if "session.jsonl.zstd" in files:
                out.append(Path(root))
    except OSError as e:
        log.warning("sessions 目录遍历失败: %s", e)
    return out


def find_session_log(session_id: str) -> Path | None:
    """按会话 id 定位日志文件（目录名包含 sessionId）。"""
    if not session_id:
        return None
    for d in _iter_session_log_dirs():
        if session_id in d.name:
            fp = d / "session.jsonl.zstd"
            if fp.is_file():
                return fp
    return None


def read_session_events(session_id: str, limit: int = 0) -> list[dict]:
    """读取会话全部原始事件（按 seq 排序；limit>0 时只取最新 limit 条）。

    返回每条形如 {"type", "seq", "time", "data"}（与 session.history 的
    event 结构一致），其中存储行（text-chunks/reasoning-chunks/tool-call-chunks）
    已通过 expand_row 展开为单条事件。
    """
    fp = find_session_log(session_id)
    if fp is None:
        return []
    try:
        data = fp.read_bytes()
    except OSError as e:
        log.warning("读取会话日志失败 %s: %s", fp, e)
        return []

    events: list[dict] = []
    scan = scan_zstd_frames(data)
    for fr in scan.frames:
        text = _decode_frame(data[fr.start:fr.end])
        if not text:
            continue
        for line in text.split("\n"):
            if not line.strip():
                continue
            for ev in expand_row(line):
                if isinstance(ev, dict) and ev.get("type"):
                    events.append(ev)

    # 按 seq 排序（会话事件带自增 seq；无 seq 的排前面）
    events.sort(key=lambda e: e.get("seq", 0))
    if limit > 0 and len(events) > limit:
        events = events[-limit:]
    return events


def read_full_record(session_id: str) -> FullRecord:
    """聚合为结构化工作记录（供高级交互历史回放）。"""
    rec = FullRecord(session_id=session_id)
    events = read_session_events(session_id)
    if not events:
        return rec
    rec.events = events

    # 当前 turn 聚合状态
    cur_turn: TurnRecord | None = None
    cur_reasoning_parts: list[str] = []

    for ev in events:
        etype = ev.get("type", "")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        t = float(ev.get("time") or 0) / 1000.0

        if etype == "session":
            rec.session_id = str(data.get("id") or rec.session_id)
            rec.cwd = str(data.get("cwd") or rec.cwd)
            rec.agent_preset = str(data.get("agentPreset") or rec.agent_preset)
            rec.created_at = float(data.get("createdAt") or 0) / 1000.0
        elif etype == "session/title":
            title = data.get("title")
            if isinstance(title, str):
                rec.title = title
        elif etype == "turn/start":
            if cur_turn is not None:
                rec.turns.append(cur_turn)
            cur_turn = TurnRecord(turn=int(data.get("turn") or 0), started_at=t)
            cur_reasoning_parts = []
        elif etype == "turn/end":
            if cur_turn is not None:
                cur_turn.ended_at = t
                reason = data.get("reason")
                if isinstance(reason, dict):
                    cur_turn.reason = str(reason.get("kind") or "")
                rec.turns.append(cur_turn)
                cur_turn = None
        elif etype == "step/start":
            if cur_turn is not None:
                cur_turn.steps += 1
        elif etype == "user/message":
            if cur_turn is not None:
                cur_turn.user_messages.append(data)
        elif etype == "assistant/message":
            # 助手消息 content 块里可能含 reasoning 内容
            if cur_turn is not None:
                cur_turn.assistant_messages.append(data)
            content = data.get("message")
            if isinstance(content, dict):
                blocks = content.get("content")
                if isinstance(blocks, list):
                    for b in blocks:
                        if isinstance(b, dict) and b.get("type") == "reasoning" and isinstance(b.get("text"), str):
                            cur_reasoning_parts.append(b["text"])
                            if cur_turn is not None:
                                cur_turn.reasoning.append(b["text"])
        elif etype == "assistant/chunk":
            chunk = data.get("chunk")
            if isinstance(chunk, dict):
                # 思考流式块（Web 版 ThinkRow 实时数据；回放时只取 reasoning-delta）
                if chunk.get("type") == "reasoning-delta" and isinstance(chunk.get("text"), str):
                    cur_reasoning_parts.append(chunk["text"])
        elif etype == "tool/call":
            call = dict(data)
            if cur_turn is not None:
                cur_turn.tool_calls.append(call)
        elif etype == "approval/asked" or etype == "approval/decided" or etype == "approval/policy":
            rec.approvals.append(dict(data))
            if cur_turn is not None:
                cur_turn.approvals.append(dict(data))
        elif etype == "todo/write":
            todos = data.get("todos")
            if isinstance(todos, list):
                rec.todos.append({"time": t, "todos": todos})
            if cur_turn is not None:
                cur_turn.todos.append(dict(data))
        elif etype == "agent/inbox/spliced":
            rec.queue_splices.append(dict(data))
        elif etype == "goal/change":
            rec.goals.append(dict(data))

    if cur_turn is not None:
        rec.turns.append(cur_turn)
    return rec


def latest_todos(rec: FullRecord) -> list[dict] | None:
    """取"当前生效"的计划：最新一次 todo/write 且其后再无 turn/start。

    对齐 Web 版 TodoDock 语义（standing plan = 最新 todo/write 且其后无 turn/start）。
    返回 None 表示无生效计划。
    """
    if not rec.todos:
        return None
    latest = rec.todos[-1]
    latest_time = latest.get("time", 0.0)
    for ev in rec.events:
        if ev.get("type") == "turn/start":
            t = float(ev.get("time") or 0) / 1000.0
            if t > latest_time:
                return None
    return latest.get("todos")
