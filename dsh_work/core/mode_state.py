"""模式状态与管理器（第 2.3 节）。

模式管理器是 DSH Work 区别于传统聊天客户端的核心组件。它驻留在 UI 层，
负责根据当前模式（Work / Code）动态调整界面布局、控件可见性和默认行为。

模式切换时，模式管理器会：
1. 保存当前会话的 UI 状态（滚动位置、面板宽度）
2. 切换布局配置
3. 切换硬编码的默认系统提示词

整个过程在 200ms 内完成，用户感知不到延迟。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .. import constants as C
from .defaults import get_skills_for_mode, get_system_prompt_for_mode


class Mode(str, Enum):
    """工作模式。"""

    WORK = C.MODE_WORK
    CODE = C.MODE_CODE


@dataclass
class PanelConfig:
    """面板配置（随模式变化）。"""

    left_content: str = "tasks"   # tasks / file_tree
    right_content: str = "preview"  # preview / tools
    show_terminal: bool = False
    tool_call_expanded: bool = False  # 工具调用卡片是否默认展开


@dataclass
class ModeState:
    """模式状态：保存当前模式与其对应的 UI 配置。"""

    mode: Mode = Mode.WORK
    panel: PanelConfig = field(default_factory=PanelConfig)
    # 当前会话的 UI 状态快照（切换时保存，切回时恢复）
    ui_snapshots: dict[str, dict] = field(default_factory=dict)

    @property
    def is_work(self) -> bool:
        return self.mode == Mode.WORK

    @property
    def is_code(self) -> bool:
        return self.mode == Mode.CODE

    @property
    def skills(self) -> list[dict]:
        return get_skills_for_mode(self.mode.value)

    @property
    def system_prompt(self) -> str:
        return get_system_prompt_for_mode(self.mode.value)

    @property
    def input_placeholder(self) -> str:
        if self.is_code:
            return "输入指令或粘贴代码..."
        return "描述你想完成的工作..."


class ModeManager:
    """模式管理器。

    管理 Work / Code 模式切换，分发模式变更事件到 UI 层。

    使用方法：
        manager = ModeManager()
        manager.switch(Mode.CODE)
        # UI 层通过 add_listener 监听切换事件
    """

    def __init__(self, initial_mode: Mode = Mode.WORK):
        self._state = ModeState(mode=initial_mode)
        self._listeners: list[Callable[[ModeState], None]] = []

    @property
    def state(self) -> ModeState:
        return self._state

    @property
    def current_mode(self) -> Mode:
        return self._state.mode

    def get_panel_config(self, mode: Mode | None = None) -> PanelConfig:
        """获取指定模式的面板配置。"""
        m = mode or self._state.mode
        if m == Mode.WORK:
            return PanelConfig(
                left_content="tasks",
                right_content="preview",
                show_terminal=False,
                tool_call_expanded=False,
            )
        return PanelConfig(
            left_content="file_tree",
            right_content="tools",
            show_terminal=True,
            tool_call_expanded=True,
        )

    def switch(self, mode: Mode, save_snapshot: bool = True) -> None:
        """切换模式。

        切换前保存当前会话的 UI 状态（滚动位置、面板宽度），
        切换后恢复目标模式的 UI 状态。
        """
        if mode == self._state.mode:
            return

        old_mode = self._state.mode
        log_debug = f"模式切换: {old_mode.value} → {mode.value}"
        from ..utils.logger import get_logger
        get_logger("core.mode_state").info(log_debug)

        # 保存旧模式的面板配置（供切回时恢复）
        self._state.ui_snapshots[old_mode.value] = {
            "panel": self._state.panel,
        }

        # 切换模式 + 加载新模式的面板配置
        self._state.mode = mode
        self._state.panel = self.get_panel_config(mode)

        # 通知监听器
        for listener in list(self._listeners):
            try:
                listener(self._state)
            except Exception:
                pass

    def add_listener(self, listener: Callable[[ModeState], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[ModeState], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def toggle(self) -> Mode:
        """在 Work / Code 之间切换。"""
        new_mode = Mode.CODE if self._state.mode == Mode.WORK else Mode.WORK
        self.switch(new_mode)
        return new_mode
