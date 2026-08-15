"""离线模式 SQLite 缓存（第 8.4 节）。

当 DSH 进程不可用时（如 Node.js 损坏、DSH 升级中），客户端进入只读模式：
可以浏览历史会话记录（从本地缓存读取），但不能发送新消息或执行工具。

缓存持久化策略：
- 持久化时机：每次 Agent Turn 完全结束（收到 turn_end / done）时，
  将该 Turn 的完整上下文序列化为 JSON，存入本地 SQLite。
- 缓存限制：仅保留最近 5000 条消息（约 500 个会话），超出按 LRU 淘汰。
- 离线读取：启动时若 DSH 不可连，直接读取 SQLite 并按时间倒序展示最近 10 条会话。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import constants as C
from ..config import get_offline_db_path
from ..utils.logger import get_logger

log = get_logger("core.offline_cache")


@dataclass
class CachedSession:
    """缓存的会话摘要。"""

    session_id: str
    title: str
    last_message_at: float
    message_count: int


@dataclass
class CachedMessage:
    """缓存的消息。"""

    session_id: str
    role: str
    content: str
    timestamp: float
    turn: int
    metadata: str  # JSON 字符串


class OfflineCache:
    """离线模式 SQLite 缓存。

    表结构：
    - sessions: session_id, title, last_message_at, message_count, accessed_at
    - messages: id, session_id, role, content, timestamp, turn, metadata
    """

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or get_offline_db_path()
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    last_message_at REAL NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    accessed_at REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    timestamp REAL NOT NULL DEFAULT 0,
                    turn INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, timestamp);
                CREATE INDEX IF NOT EXISTS idx_sessions_accessed
                    ON sessions(accessed_at);
            """)
            conn.commit()

    def save_turn(
        self,
        session_id: str,
        title: str,
        user_message: str,
        assistant_message: str,
        tool_summary: list[dict] | None = None,
        turn: int = 0,
    ) -> None:
        """Agent Turn 结束时持久化完整上下文。

        Args:
            session_id: 会话 ID
            title: 会话标题
            user_message: 用户提问
            assistant_message: Assistant 最终回复
            tool_summary: 工具调用摘要
            turn: Turn 序号
        """
        now = time.time()
        metadata = json.dumps({"tool_summary": tool_summary or []}, ensure_ascii=False)

        with sqlite3.connect(self.db_path) as conn:
            # 写入用户消息
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, turn, metadata) "
                "VALUES (?, 'user', ?, ?, ?, '{}')",
                (session_id, user_message, now, turn),
            )
            # 写入 Assistant 消息
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, turn, metadata) "
                "VALUES (?, 'assistant', ?, ?, ?, ?)",
                (session_id, assistant_message, now + 0.001, turn, metadata),
            )
            # 更新会话摘要
            conn.execute(
                """
                INSERT INTO sessions (session_id, title, last_message_at, message_count, accessed_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title,
                    last_message_at = excluded.last_message_at,
                    message_count = message_count + 2,
                    accessed_at = excluded.accessed_at
                """,
                (session_id, title, now, now),
            )
            conn.commit()

        # LRU 淘汰
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        """LRU 淘汰：仅保留最近 5000 条消息。"""
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if count <= C.OFFLINE_CACHE_MAX_MESSAGES:
                return
            # 删除最旧的消息
            excess = count - C.OFFLINE_CACHE_MAX_MESSAGES
            conn.execute(
                "DELETE FROM messages WHERE id IN "
                "(SELECT id FROM messages ORDER BY timestamp ASC LIMIT ?)",
                (excess,),
            )
            # 清理空会话
            conn.execute(
                "DELETE FROM sessions WHERE session_id NOT IN "
                "(SELECT DISTINCT session_id FROM messages)"
            )
            conn.commit()
            log.info("LRU 淘汰 %d 条过期消息", excess)

    def list_recent_sessions(self, limit: int = 10) -> list[CachedSession]:
        """列出最近会话（离线模式启动时用）。"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT session_id, title, last_message_at, message_count "
                "FROM sessions ORDER BY last_message_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            CachedSession(
                session_id=r[0],
                title=r[1],
                last_message_at=r[2],
                message_count=r[3],
            )
            for r in rows
        ]

    def get_messages(self, session_id: str, limit: int = 50) -> list[CachedMessage]:
        """获取会话的缓存消息。"""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT session_id, role, content, timestamp, turn, metadata "
                "FROM messages WHERE session_id = ? ORDER BY timestamp ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [
            CachedMessage(
                session_id=r[0],
                role=r[1],
                content=r[2],
                timestamp=r[3],
                turn=r[4],
                metadata=r[5],
            )
            for r in rows
        ]

    def touch_session(self, session_id: str) -> None:
        """更新会话访问时间（LRU 用）。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET accessed_at = ? WHERE session_id = ?",
                (time.time(), session_id),
            )
            conn.commit()

    def clear_all(self) -> None:
        """清空所有缓存。"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.commit()
        log.info("离线缓存已清空")
