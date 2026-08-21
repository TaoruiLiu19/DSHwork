"""业务逻辑层。

处理会话状态、模式管理、技能加载、进程管理。
依赖通信层（dsh_work.api），向上为 UI 层提供纯 Python 接口。
"""

from .defaults import DEFAULT_SKILLS, DEFAULT_SYSTEM_PROMPTS, EMPTY_STATE_CARDS, SCENARIO_CARDS
from .mode_state import ModeManager, ModeState
from .offline_cache import OfflineCache
from .process_manager import ProcessManager, ProcessOwnership
from .renderer_recovery import RecoveryAction, RecoveryStats, RendererRecoveryMachine
from .safety_guard import (
    DANGEROUS_EXT_DESC,
    DANGEROUS_EXT_PATTERN,
    SafetyVerdict,
    build_roots_from_context,
    can_open_or_restore,
    can_write_workspace,
    is_dangerous_ext,
    is_within_roots,
    parse_href_path,
    sanitize_file_href,
)
from .session_manager import SessionManager, SessionState
from .session_watcher import SessionWatcher

__all__ = [
    "DEFAULT_SKILLS",
    "DEFAULT_SYSTEM_PROMPTS",
    "EMPTY_STATE_CARDS",
    "SCENARIO_CARDS",
    "ModeState",
    "ModeManager",
    "SessionManager",
    "SessionState",
    "ProcessManager",
    "ProcessOwnership",
    "OfflineCache",
    "SessionWatcher",
    "SafetyVerdict",
    "DANGEROUS_EXT_PATTERN",
    "DANGEROUS_EXT_DESC",
    "is_dangerous_ext",
    "is_within_roots",
    "build_roots_from_context",
    "can_open_or_restore",
    "can_write_workspace",
    "sanitize_file_href",
    "parse_href_path",
    "RendererRecoveryMachine",
    "RecoveryAction",
    "RecoveryStats",
]
