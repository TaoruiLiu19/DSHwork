"""业务逻辑层。

处理会话状态、模式管理、技能加载、进程管理。
依赖通信层（dsh_work.api），向上为 UI 层提供纯 Python 接口。
"""

from .defaults import DEFAULT_SKILLS, DEFAULT_SYSTEM_PROMPTS, EMPTY_STATE_CARDS, SCENARIO_CARDS
from .mode_state import ModeState, ModeManager
from .session_manager import SessionManager, SessionState
from .process_manager import ProcessManager, ProcessOwnership
from .offline_cache import OfflineCache
from .session_watcher import SessionWatcher
from .safety_guard import (
    SafetyVerdict,
    DANGEROUS_EXT_PATTERN,
    DANGEROUS_EXT_DESC,
    is_dangerous_ext,
    is_within_roots,
    build_roots_from_context,
    can_open_or_restore,
    can_write_workspace,
    sanitize_file_href,
    parse_href_path,
)
from .renderer_recovery import RendererRecoveryMachine, RecoveryAction, RecoveryStats

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
