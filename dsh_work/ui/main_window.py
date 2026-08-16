"""主窗口：TRAE Work 风格布局。

布局结构：
┌─────────────────────────────────────────────────┐
│  TopStrip (36px): 工作区 | 模型选择 | 新建会话    │
├──────┬────────┬─────────────────┬───────────────┤
│      │        │                 │               │
│  A   │ 二级   │  对话/工作区     │  预览/工具    │
│  c   │ 面板   │  (消息流+输入)   │               │
│  t   │        │                 │               │
│  B   │        │                 │               │
│  a   │        │                 │               │
│  r   │        │                 │               │
│      │        │                 │               │
├──────┴────────┴─────────────────┴───────────────┤
│  StatusBar (24px): 连接 | Agent | 上下文 | 版本   │
└─────────────────────────────────────────────────┘

- ActivityBar (48px): 图标导航 + 模式切换 + 设置
- 二级面板 (200px): 会话/文件/搜索/Git（由 ActivityBar 控制）
- 中栏 (flex): 消息流 + 输入框
- 右栏 (220px): 预览/工具调用

快捷键体系不变。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QKeySequence, QShortcut, QCloseEvent
from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QSplitter,
    QApplication,
    QTabWidget,
)

from .. import constants as C
from ..api import CompatibilityMode, DshService
from ..config import UserConfig
from ..core.mode_state import Mode, ModeManager
from ..core.session_manager import SessionManager, AgentStatus
from ..utils.logger import get_logger
from .panels.left_panel import LeftPanel
from .panels.center_panel import CenterPanel
from .panels.right_panel import RightPanel
from .panels.usage_panel import UsagePanel
from .status_bar import StatusBar
from .system_tray import SystemTray
from .title_bar import TitleBar
from .widgets.activity_bar import ActivityBar

log = get_logger("ui.main_window")


class MainWindow(QMainWindow):
    """DSH Work 主窗口（TRAE Work 风格布局）。"""

    quit_requested = Signal()

    def __init__(self, dsh: DshService, parent: QWidget | None = None):
        super().__init__(parent)
        self.dsh = dsh
        self.config = UserConfig.load()

        # 核心组件
        self.mode_manager = ModeManager(Mode(self.config.mode))
        self.session_manager = SessionManager(dsh)
        self.system_tray = SystemTray(self)

        # UI 组件
        self.title_bar = TitleBar(self)
        self.activity_bar = ActivityBar(self)
        self.left_panel = LeftPanel(self)
        self.center_panel = CenterPanel(self)
        self.usage_panel = UsagePanel(self.dsh.usage, self.dsh.balance)
        self.right_panel = RightPanel(self)
        self.status_bar = StatusBar(self)

        self._setup_window()
        self._setup_layout()
        self._setup_shortcuts()
        self._connect_signals()
        self._apply_config()

        # 注册主题监听器：主题切换时刷新中心 Tab 样式（documentMode 下全局 QSS 选择器不可靠）
        from .theme.theme_manager import ThemeManager
        tm = ThemeManager()
        tm.add_listener(self._on_theme_changed)
        # 首次应用当前主题到中心 Tab
        if tm._current:
            self._on_theme_changed(tm._current)

        log.info("主窗口已初始化 mode=%s", self.config.mode)

    def _setup_window(self) -> None:
        self.setObjectName("MainWindow")
        self.setWindowTitle(C.APP_NAME)
        self.resize(1280, 800)
        self.setMinimumSize(900, 600)

    def _setup_layout(self) -> None:
        """TRAE Work 风格布局：TopStrip | (ActivityBar + Splitter) | StatusBar。"""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 顶部极简栏
        main_layout.addWidget(self.title_bar)

        # 中间区域：ActivityBar + 三栏 Splitter
        mid_layout = QHBoxLayout()
        mid_layout.setContentsMargins(0, 0, 0, 0)
        mid_layout.setSpacing(0)

        # ActivityBar（固定窄边栏）
        mid_layout.addWidget(self.activity_bar)

        # 三栏分割器
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self.left_panel)

        # 中心 Tab：「对话」 / 「用量与消耗」（与 DSH WebUI 的 Tab 体系一致）
        self._center_tabs = QTabWidget()
        # 不用 documentMode：Windows 下 documentMode 会启用原生绘制，导致 Tab 栏背景白色、不受 QSS 控制
        self._center_tabs.setTabPosition(QTabWidget.TabPosition.North)
        # Tab 样式由 _on_theme_changed 主题监听器直接 setStyleSheet，随主题切换
        self._center_tabs.addTab(self.center_panel, "💬 对话")
        self._center_tabs.addTab(self.usage_panel, "📊 用量与消耗")
        self._splitter.addWidget(self._center_tabs)

        self._splitter.addWidget(self.right_panel)

        # 应用面板宽度比例（二级面板更窄，中栏更宽）
        ratios = self.config.panel_ratios
        total_width = self.width() - 48  # 减去 ActivityBar 宽度
        self._splitter.setSizes([
            int(total_width * ratios.get("left", 0.16)),
            int(total_width * ratios.get("center", 0.62)),
            int(total_width * ratios.get("right", 0.22)),
        ])
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

        mid_layout.addWidget(self._splitter, stretch=1)

        # 将 mid_layout 包装为 QWidget 添加到主布局
        mid_widget = QWidget()
        mid_widget.setLayout(mid_layout)
        main_layout.addWidget(mid_widget, stretch=1)

        # 底部状态栏
        main_layout.addWidget(self.status_bar)

    def _setup_shortcuts(self) -> None:
        self._add_shortcut("Ctrl+N", self._action_new_session)
        self._add_shortcut("Ctrl+Shift+F", self._action_search_sessions)
        self._add_shortcut("Ctrl+B", self._toggle_left_panel)
        self._add_shortcut("Ctrl+J", self._toggle_right_panel)
        self._add_shortcut("Ctrl+Shift+M", self._action_toggle_preset)
        self._add_shortcut("Ctrl+.", self._action_toggle_theme)
        self._add_shortcut("Ctrl+Return", self._action_send)
        self._add_shortcut("Esc", self._action_stop)
        self._add_shortcut("Ctrl+,", self._action_open_settings)

    def _add_shortcut(self, sequence: str, callback) -> None:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.activated.connect(callback)

    def _connect_signals(self) -> None:
        # 顶栏：Agent Preset 切换 + 模型选择 + 工作区
        self.title_bar.preset_changed.connect(self._on_preset_changed)
        self.title_bar.model_changed.connect(self._on_model_changed)
        self.title_bar.workspace_clicked.connect(self._action_change_workspace)

        # ActivityBar（导航 + 设置 + 主题）
        self.activity_bar.nav_changed.connect(self._on_nav_changed)
        self.activity_bar.settings_clicked.connect(self._action_open_settings)
        self.activity_bar.theme_clicked.connect(self._action_toggle_theme)

        # 中栏
        self.center_panel.send_requested.connect(self._on_send)
        self.center_panel.stop_requested.connect(self._action_stop)
        self.center_panel.card_clicked.connect(self._on_card_clicked)
        self.center_panel.scrolled_to_top.connect(self._on_scrolled_to_top)
        self.center_panel.files_dropped.connect(self._on_files_dropped)
        # 余额内联小部件：点击刷新
        self.center_panel.balance_refresh_requested.connect(self._action_refresh_balance)

        # 左栏：新建会话按钮 + 关闭面板 + 会话切换 + 文件双击 + 删除/重命名
        self.left_panel.new_session_requested.connect(self._action_new_session)
        self.left_panel.session_selected.connect(self._on_session_selected)
        self.left_panel.file_activated.connect(self._on_file_activated)
        self.left_panel.session_delete_requested.connect(self._on_session_delete)
        self.left_panel.session_rename_requested.connect(self._on_session_rename)
        self.left_panel.close_requested.connect(lambda: self._toggle_panel("left"))

        # 右栏：关闭面板
        self.right_panel.close_requested.connect(lambda: self._toggle_panel("right"))

        # 模式管理器（兼容旧逻辑：preset→mode 映射后走 mode_manager 分发）
        self.mode_manager.add_listener(self._on_mode_state_changed)

        # 会话管理器
        self.session_manager.add_listener(self._on_session_changed)

        # 系统托盘
        self.system_tray.new_session_requested.connect(self._action_new_session)
        self.system_tray.toggle_mode_requested.connect(self._action_toggle_preset)
        self.system_tray.quit_requested.connect(self._on_quit)
        self.system_tray.restore_requested.connect(self._on_restore)

    def _apply_config(self) -> None:
        # Agent Preset
        preset_id = getattr(self.config, "preset", None)
        # 兼容旧配置：只有 mode 时按映射转成 preset
        if not preset_id:
            preset_id = self._mode_to_preset(getattr(self.config, "mode", "work"))
        # force=True：首次初始化必须设置状态栏/标题栏 preset，不走"相同就跳过"
        self._on_preset_changed(preset_id, force=True)

        # 工作区
        self.title_bar.set_workspace(self.config.workspace)
        if self.config.workspace:
            self.left_panel.load_workspace(self.config.workspace)

        # 面板折叠状态
        if self.config.panel_collapsed.get("left"):
            self.left_panel.setVisible(False)
        if self.config.panel_collapsed.get("right"):
            self.right_panel.setVisible(False)

        # 系统托盘
        if self.config.minimize_to_tray:
            self.system_tray.setup()
            # 点击系统通知 → 激活窗口（与 Electron 版一致）
            tray = getattr(self.system_tray, "_tray", None)
            if tray is not None:
                try:
                    tray.messageClicked.connect(self._on_restore)
                except Exception:  # 老版 Qt 没有该信号时静默
                    pass

        # 余额内联小部件：初始化显示 + 延迟首次查询（避免与 DSH 初始化抢资源）
        self.center_panel.set_turn_cost(0.0, 0.0)
        self.center_panel.set_balance_loading()
        QTimer.singleShot(
            C.UPDATE_CHECK_DELAY_MS,
            lambda: self._query_balance_async(force=False),
        )

    @staticmethod
    def _mode_to_preset(mode: str) -> str:
        """兼容旧 mode 字段（work/code）→ preset id。"""
        return "code" if mode == "code" else "standard"

    @staticmethod
    def _preset_to_mode(preset_id: str) -> str:
        """preset → 面板布局 mode（standard/minimal/cordis→work，code→code）。"""
        return C.MODE_CODE if preset_id == "code" else C.MODE_WORK

    # ===== ActivityBar 导航 =====

    def _on_nav_changed(self, nav: str) -> None:
        """ActivityBar 导航切换：更新二级面板内容，并自动显示左栏。"""
        if not self.left_panel.isVisible():
            self.left_panel.setVisible(True)
            self.config.panel_collapsed["left"] = False
            self.config.save()
        self.left_panel.set_nav(nav)
        log.debug("导航切换: %s", nav)

    # ===== Agent Preset 切换 =====

    def _on_preset_changed(self, preset_id: str, force: bool = False) -> None:
        """切换 Agent Preset：保存配置 + 同步 UI + 发切换日志。

        Args:
            preset_id: 目标 preset id（standard/code/minimal/cordis）
            force: 强制同步 UI（初次初始化时用，避免相同 preset 直接 return 导致状态栏/标题栏空白）
        """
        if not preset_id:
            return
        from .title_bar import TitleBar
        preset_name = dict(TitleBar.PRESETS).get(preset_id, preset_id)
        current_preset = getattr(self.config, "preset", None) or self._mode_to_preset(self.config.mode)
        if (not force) and current_preset == preset_id:
            # 仅在非 force 时短路；force 必须完整跑一遍以同步标题栏和状态栏
            # （因为启动时 UI 控件还没有 preset 名称/颜色）
            return

        mode_val = self._preset_to_mode(preset_id)
        current_mode = getattr(self.config, "mode", C.MODE_WORK)
        # 记录 Preset 级别的切换日志（清晰展示 4 种 preset 间的变化）
        if current_mode != mode_val:
            log.info("Preset 切换: %s → %s (布局 %s → %s)",
                     dict(TitleBar.PRESETS).get(current_preset, current_preset),
                     preset_name,
                     current_mode, mode_val)
        else:
            log.info("Preset 切换: %s → %s (布局保持 %s)",
                     dict(TitleBar.PRESETS).get(current_preset, current_preset),
                     preset_name, mode_val)

        self.mode_manager.switch(Mode(mode_val))
        self.config.mode = mode_val
        try:
            self.config.preset = preset_id
        except Exception:
            pass
        self.config.save()

        # 同步 UI
        self.right_panel.set_mode(mode_val)
        self.center_panel.set_mode(mode_val)
        self.status_bar.set_preset(preset_id, preset_name)
        self.title_bar.set_preset(preset_id)
        # 状态栏额外显示当前 Preset 中文名（右侧临时提示）
        self.status_bar.show_temporary(f"已切换至: {preset_name}", 2500)

    def _on_mode_state_changed(self, state) -> None:
        log.debug("模式状态已更新: %s", state.mode.value)

    def _action_toggle_preset(self) -> None:
        """循环切换下一个 preset（标准 → PTC → 极简 → 创造 → 标准…）。"""
        from .title_bar import TitleBar
        current_preset = getattr(self.config, "preset", None) or self._mode_to_preset(self.config.mode)
        ids = [pid for pid, _ in TitleBar.PRESETS]
        try:
            idx = ids.index(current_preset)
        except ValueError:
            idx = 0
        next_idx = (idx + 1) % len(ids)
        self._on_preset_changed(ids[next_idx])

    # ===== 会话管理 =====

    def _action_new_session(self) -> None:
        try:
            preset_id = getattr(self.config, "preset", None) or self._mode_to_preset(self.config.mode)
            state = self.session_manager.create_session(
                title="新会话",
                model=self.config.last_model,
                workspace=self.config.workspace,
                agent_preset=preset_id,
            )
            self.center_panel.clear_messages()
            # 刷新左栏会话列表
            infos = [s.info for s in self.session_manager.sessions.values()]
            self.left_panel.refresh_sessions(infos)
            # 提示用户：如果是本地草稿会话（DSH 不可用时的回退）
            if getattr(state, "local_draft", False):
                self.status_bar.show_temporary(
                    "📝 DSH 离线：已创建本地草稿会话，恢复后可同步",
                    color="#FFB454", duration_ms=4500,
                )
                self.center_panel.show_hint(
                    "当前为离线草稿会话",
                    "DSH 未连接，消息暂存本地。连接恢复后发送第一条消息时会自动同步。",
                )
            else:
                self.status_bar.show_temporary(
                    "✨ 新会话已创建", color="#32F08C", duration_ms=1800
                )
        except Exception as e:
            log.error("新建会话失败: %s", e)
            self.status_bar.show_temporary(
                "❌ 新建会话失败: %s" % e, color="#FF6B6B", duration_ms=5000
            )

    def _action_search_sessions(self) -> None:
        """搜索会话：切换到搜索面板。"""
        self.activity_bar.set_nav("search")
        log.info("切换到搜索面板")

    def _on_session_selected(self, session_id: str) -> None:
        """左栏点击会话项：切换并重载消息流。"""
        if not session_id:
            return
        if session_id == self.session_manager.current_session_id:
            return
        self.session_manager.switch_to(session_id)
        # 切换后渲染历史消息
        state = self.session_manager.current_session
        if state:
            if state.messages:
                self.center_panel.load_history(state.messages)
            else:
                self.center_panel.clear_messages()
            # 同步状态栏
            self._on_session_changed(session_id, state)
        log.info("已切换到会话: %s", session_id)

    def _on_file_activated(self, file_path: str) -> None:
        """左栏文件树双击：在右栏预览文件（含安全围栏拦截）。

        安全围栏对齐 dsh_desktop main.js：
          · 危险扩展名黑名单（.bat/.exe/.ps1/.vbs/.lnk/.reg …）一律拒绝
          · 超出「工作区 + 会话 cwd」根目录的路径一律拒绝（防止 Startup\\*.bat 这类越权）
        """
        if not file_path:
            return

        # ---------- 安全围栏 ----------
        from ..core.safety_guard import (
            can_open_or_restore,
            build_roots_from_context,
            DANGEROUS_EXT_DESC,
        )
        session_cwd = ""
        try:
            cur = self.session_manager.current_session
            if cur is not None and cur.info is not None:
                info_cwd = getattr(cur.info, "cwd", None) or ""
                if isinstance(info_cwd, str):
                    session_cwd = info_cwd
        except Exception:
            session_cwd = ""
        roots = build_roots_from_context(
            workspace=self.config.workspace, session_cwd=session_cwd,
        )
        verdict = can_open_or_restore(file_path, roots)
        if not verdict.allowed:
            if verdict.blocked_by_ext:
                self.status_bar.show_temporary(
                    "⛔ " + DANGEROUS_EXT_DESC, color="#FF6B6B", duration_ms=6000,
                )
            elif verdict.blocked_by_roots:
                self.status_bar.show_temporary(
                    "⛔ 安全限制：只允许预览工作区/会话目录之内的文件（防越权写入）",
                    color="#FF6B6B", duration_ms=6000,
                )
            else:
                self.status_bar.show_temporary(
                    "⛔ 已阻止该文件的预览请求", color="#FF6B6B", duration_ms=4000,
                )
            log.warning("安全围栏：拦截文件预览 %s (roots=%s)", file_path, roots)
            return

        # 确保右栏可见
        if not self.right_panel.isVisible():
            self.right_panel.setVisible(True)
            self.config.panel_collapsed["right"] = False
            self.config.save()
        self.right_panel.preview_file(file_path)
        log.debug("预览文件: %s", file_path)

    def _on_files_dropped(self, files: list) -> None:
        """拖拽文件到输入框：暂存附件路径，发送时一并提交。

        当前实现：把文件路径作为附件列表暂存到当前 pending_attachments，
        发送消息时通过 SessionManager.send_message(text, attachments) 一起发出。
        """
        if not files:
            return
        if not hasattr(self, "_pending_attachments"):
            self._pending_attachments = []
        self._pending_attachments.extend(files)
        names = ", ".join(f.split("/")[-1].split("\\")[-1] for f in files)
        self.status_bar.show_temporary(
            "📎 已附加: %s" % names, color="#387BFF", duration_ms=2500
        )
        log.info("附件暂存: %s", files)

    def _on_session_delete(self, session_id: str) -> None:
        """右键删除会话：确认后调用 DSH + 刷新左栏。"""
        from PySide6.QtWidgets import QMessageBox
        state = self.session_manager.sessions.get(session_id)
        title = state.info.title if state else session_id
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("删除会话")
        box.setText(f"确定删除会话「{title}」吗？")
        box.setInformativeText("删除后无法恢复。")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        btn_yes = box.button(QMessageBox.StandardButton.Yes)
        btn_yes.setText("删除")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes.value:
            return
        dsh_ok = self.session_manager.delete_session(session_id)
        # 刷新左栏列表
        infos = [s.info for s in self.session_manager.sessions.values()]
        self.left_panel.refresh_sessions(infos)
        # 若删除的是当前会话，清空中栏
        if not self.session_manager.current_session_id:
            self.center_panel.clear_messages()
        self.status_bar.show_temporary(
            "🗑 已删除" if dsh_ok else "🗑 已删除（DSH 端失败，仅本地）",
            color="#FF6B6B" if dsh_ok else "#FFB454", duration_ms=2500,
        )

    def _on_session_rename(self, session_id: str) -> None:
        """右键重命名会话：输入框确认后调用 DSH + 刷新左栏。"""
        from PySide6.QtWidgets import QInputDialog
        state = self.session_manager.sessions.get(session_id)
        current_title = state.info.title if state else ""
        new_title, ok = QInputDialog.getText(
            self, "重命名会话", "新名称：", text=current_title
        )
        if not ok or not new_title.strip():
            return
        new_title = new_title.strip()
        dsh_ok = self.session_manager.rename_session(session_id, new_title)
        # 刷新左栏列表
        infos = [s.info for s in self.session_manager.sessions.values()]
        self.left_panel.refresh_sessions(infos)
        self.status_bar.show_temporary(
            "✏️ 已重命名" if dsh_ok else "✏️ 已重命名（DSH 端失败，仅本地）",
            color="#32F08C" if dsh_ok else "#FFB454", duration_ms=2500,
        )

    def _on_session_changed(self, session_id: str, state) -> None:
        # 通用状态同步（每次事件都更新）
        self.center_panel.set_agent_status(state.agent_status)
        self.status_bar.set_agent_status(
            state.agent_status, state.current_turn, state.current_step
        )
        self.system_tray.set_agent_status(
            state.agent_status,
            session_title=state.info.title,
            step_text=state.step_description,
        )
        self.center_panel.set_context_usage(state.context)
        self.status_bar.set_context_usage(state.context)
        self.status_bar.set_token_usage(
            state.context.prompt_tokens, state.context.completion_tokens
        )
        # 余额内联小部件：本轮消耗 + 会话累计
        self.center_panel.set_turn_cost(state.last_turn_cost, state.session_total_cost)

        # 事件特定路由：根据 last_event_type 把数据送到对应 UI 组件
        # （修复前所有事件只更新状态栏，消息流/工具卡片/预览均不刷新）
        event_type = getattr(state, "last_event_type", "")
        if not event_type:
            return
        data = getattr(state, "last_event_data", {}) or {}

        if event_type == "chunk":
            # 流式文本块 → 追加到当前流式气泡
            chunk = data.get("chunk", "")
            if chunk:
                if not self.center_panel.is_streaming():
                    # 重连后中途收到 chunk：补创建流式气泡
                    self.center_panel.start_streaming()
                self.center_panel.append_chunk(chunk)

        elif event_type == "turn_start":
            # 新一轮开始：确保有流式气泡
            if not self.center_panel.is_streaming():
                self.center_panel.start_streaming()

        elif event_type == "turn_end":
            # 一轮结束：定稿流式气泡
            finalized = data.get("message")
            if self.center_panel.is_streaming():
                self.center_panel.finish_streaming()
                # 若本轮无任何内容（空回复），把气泡内容设为占位提示
                if finalized is None:
                    self.center_panel.fill_prompt("")  # 仅刷新布局
            elif finalized is not None:
                # 无流式气泡但有固化消息（重连后收到 turn_end）：直接添加
                self.center_panel.add_message(finalized)
            # 对话结束：后台异步刷新余额（非强制，命中5分钟缓存则零开销）
            self._query_balance_async(force=False)

        elif event_type == "tool_call":
            tool_name = data.get("tool_name", "unknown")
            params = data.get("params", {}) or {}
            self.center_panel.on_tool_call(tool_name, params)
            # 右栏 Code 模式时间线同步
            self.right_panel.add_tool_call(tool_name, "running", 0)

        elif event_type == "tool_result":
            tool_name = data.get("tool_name", "")
            status = data.get("status", "success")
            result = data.get("result")
            error = data.get("error", "")
            self.center_panel.on_tool_result(tool_name, status, result, error)
            # 右栏时间线追加结果项
            self.right_panel.add_tool_call(tool_name, status, 0)
            # 若工具产生 diff 文本，同步到 DiffView
            if isinstance(result, str) and ("\n+" in result or "\n-" in result):
                self.right_panel.show_diff(result)

        elif event_type == "error":
            # 错误：定稿未完成的流式气泡，避免界面留白
            if self.center_panel.is_streaming():
                self.center_panel.finish_streaming()
            err_msg = data.get("message") or data.get("error") or "发生未知错误"
            self.status_bar.show_temporary(
                "⚠️ %s" % err_msg, color="#F65A5A", duration_ms=5000
            )

        elif event_type == "history_sync":
            # 重连增量恢复：重载消息流（断线期间错过的消息已合并到 state.messages）
            self.center_panel.load_history(state.messages)
            added = data.get("added", 0)
            if added:
                self.status_bar.show_temporary(
                    "🔄 已恢复 %d 条断线消息" % added, color="#387BFF", duration_ms=3000
                )

    # ===== 消息发送 =====

    def _on_send(self, text: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        # ---- 防呆检查 1：DSH 连接状态 ----
        if self.dsh is None or self.dsh.is_offline:
            self._show_error_dialog(
                "DSH 未连接",
                "当前 DSH 服务处于离线状态，无法发送消息。\n\n"
                "可能原因：\n"
                "  • DSH 进程未启动或启动失败\n"
                "  • 网络断开，无法访问本地 3080 端口\n"
                "\n请先确认 DSH 正常运行后再重试。",
                "network-offline",
            )
            return

        # ---- 防呆检查 2：API Key 配置 ----
        creds = {}
        try:
            creds = self.dsh.check_credentials() or {}
        except Exception as e:
            log.warning("凭据检查异常: %s", e)
        has_key = bool(creds.get("has_key") or creds.get("configured"))
        key_valid = bool(creds.get("valid"))

        if not has_key:
            # 未配置 API Key → 弹窗引导
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("缺少 API Key")
            box.setText("尚未配置 DeepSeek API Key，无法发送消息。")
            box.setInformativeText(
                "请在「设置」中输入 API Key 后再发送。\n"
                "获取地址：https://platform.deepseek.com/"
            )
            box.setStandardButtons(
                QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel
            )
            btn_open = box.button(QMessageBox.StandardButton.Open)
            btn_open.setText("打开设置")
            box.setDefaultButton(QMessageBox.StandardButton.Open)
            ret = box.exec()
            if ret == QMessageBox.StandardButton.Open.value:
                self._action_open_settings()
            return

        if has_key and not key_valid:
            # 配置了但无效 → 警告
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("API Key 无效")
            box.setText("当前 API Key 无法通过 DSH 校验，发送可能会失败。")
            box.setInformativeText(
                "可能原因：\n"
                "  • Key 粘贴错误或已过期\n"
                "  • 账号额度不足\n"
                "\n是否仍要继续发送？"
            )
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
            )
            btn_yes = box.button(QMessageBox.StandardButton.Yes)
            btn_yes.setText("仍然发送")
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if box.exec() != QMessageBox.StandardButton.Yes.value:
                return

        # ---- 检查通过，执行发送 ----
        from ..api import MessageRecord
        import time as _time

        local_msg = MessageRecord(role="user", content=text, timestamp=_time.time())
        self.center_panel.add_message(local_msg)
        self.center_panel.start_streaming()

        # 记录插入前的消息数量（便于失败回滚）
        state = self.session_manager.current_session
        prev_count = len(state.messages) if state else 0

        # 取出暂存的附件（拖拽文件时收集）
        attachments = getattr(self, "_pending_attachments", None)
        self.session_manager.send_message(text, attachments=attachments)
        # 发送后清空附件暂存
        self._pending_attachments = []

        # ---- 发送后检查：失败时回滚 UI ----
        new_state = self.session_manager.current_session
        if new_state and new_state.agent_status == AgentStatus.ERROR:
            # RPC 抛出了业务错误，检查是否真的成功发送
            actually_sent = False
            try:
                actually_sent = getattr(self.session_manager, "_last_send_ok", False)
            except Exception:
                pass
            if not actually_sent:
                # 回滚：移除乐观添加的用户消息
                if new_state and len(new_state.messages) > prev_count:
                    new_state.messages.pop()
                # 恢复空状态（避免界面变白）
                self.center_panel.clear_messages()
                # 填回用户输入，让用户能重新点发送
                self.center_panel.fill_prompt(text)
                new_state.agent_status = AgentStatus.IDLE
                self._show_error_dialog(
                    "发送失败",
                    "DSH 服务拒绝了本次发送请求。\n\n"
                    "常见原因：\n"
                    "  • 工作区路径不存在（请在右上角重新选择工作区）\n"
                    "  • API Key 格式错误或额度耗尽\n"
                    "  • DSH 版本不匹配导致 payload 校验失败\n",
                    "rpc-error",
                )

    def _show_error_dialog(self, title: str, body: str, reason: str = "") -> None:
        """统一的错误弹窗 + 状态栏提示。"""
        from PySide6.QtWidgets import QMessageBox

        log.warning("[防呆] 阻止发送: %s reason=%s", title, reason)
        self.status_bar.show_temporary(
            "⚠️ %s" % title, color="#F65A5A", duration_ms=4000
        )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(title)
        box.setInformativeText(body)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

    def _action_send(self) -> None:
        """Ctrl+Return 快捷键：触发输入框发送。"""
        self.center_panel.trigger_send()

    def _action_stop(self) -> None:
        self.session_manager.cancel_agent()

    # ===== 空状态卡片 =====

    def _on_card_clicked(self, prompt: str, mode: str) -> None:
        target_preset = self._mode_to_preset(mode)
        current_preset = getattr(self.config, "preset", None) or self._mode_to_preset(self.config.mode)
        if target_preset != current_preset:
            self._on_preset_changed(target_preset)
        self.center_panel.fill_prompt(prompt)

    # ===== 模型切换 =====

    def _on_model_changed(self, model: str) -> None:
        if not model:
            return
        self.config.last_model = model
        self.config.save()
        if self.session_manager.current_session_id:
            self.session_manager.select_model(model)

    # ===== 面板控制 =====

    def _toggle_left_panel(self) -> None:
        self._toggle_panel("left")

    def _toggle_right_panel(self) -> None:
        self._toggle_panel("right")

    def _toggle_panel(self, side: str) -> None:
        """切换左/右面板可见性（纯净界面模式）。"""
        if side == "left":
            visible = not self.left_panel.isVisible()
            self.left_panel.setVisible(visible)
            self.config.panel_collapsed["left"] = not visible
        else:
            visible = not self.right_panel.isVisible()
            self.right_panel.setVisible(visible)
            self.config.panel_collapsed["right"] = not visible
        self.config.save()

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        sizes = self._splitter.sizes()
        total = sum(sizes) or 1
        self.config.panel_ratios = {
            "left": sizes[0] / total,
            "center": sizes[1] / total,
            "right": sizes[2] / total if len(sizes) > 2 else 0,
        }
        self.config.save()

    # ===== 主题 =====

    def _on_theme_changed(self, theme) -> None:
        """主题切换监听器：直接刷新中心 Tab 样式。"""
        c = theme.colors
        self._center_tabs.setStyleSheet(f"""
            QTabWidget {{ background-color: {c.bg_primary}; border: none; }}
            QTabWidget::pane {{ border: none; background-color: {c.bg_primary}; top: 0; }}
            QTabBar {{ background-color: {c.bg_primary}; }}
            QTabBar::tab {{
                padding: 8px 20px; font-size: 13px;
                color: {c.text_secondary};
                background: transparent;
                border: none;
                border-bottom: 2px solid transparent;
                min-height: 32px;
            }}
            QTabBar::tab:selected {{
                color: {c.accent};
                border-bottom-color: {c.accent};
                font-weight: 600;
            }}
            QTabBar::tab:hover:!selected {{
                color: {c.text_primary};
                background-color: {c.bg_hover};
            }}
        """)

    def _action_toggle_theme(self) -> None:
        from .theme.theme_manager import ThemeManager
        tm = ThemeManager()
        tm.load_all()
        current = self.config.theme
        # 循环切换所有已加载主题（与设置面板下拉列表一致）
        all_keys = list(tm.theme_keys.keys())
        if not all_keys:
            return
        if current in all_keys:
            idx = all_keys.index(current)
            new_theme = all_keys[(idx + 1) % len(all_keys)]
        else:
            new_theme = all_keys[0]
        theme = tm.set_current(new_theme)
        if theme:
            self.config.theme = new_theme
            self.config.save()
            qss = tm.generate_qss(theme)
            QApplication.instance().setStyleSheet(qss)

    # ===== 设置 =====

    def _action_open_settings(self) -> None:
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
            QLineEdit, QPushButton, QCheckBox, QComboBox,
            QGroupBox, QPlainTextEdit, QDialogButtonBox, QWidget,
        )

        log.info("打开设置")
        dlg = QDialog(self)
        dlg.setWindowTitle("设置")
        dlg.resize(540, 640)

        # 应用当前主题 QSS 到设置对话框，使设置面板跟随主题变色
        from .theme.theme_manager import ThemeManager
        tm = ThemeManager()
        tm.load_all()
        _theme = tm.current or tm.get_theme(self.config.theme)
        if _theme:
            # generate_qss 未覆盖 QDialog 背景，需追加，否则窗口底部仍是系统默认浅色
            _dlg_qss = tm.generate_qss(_theme) + (
                f"\nQDialog {{ background-color: {_theme.colors.bg_secondary}; }}"
                f"\nQDialog QGroupBox {{ background-color: {_theme.colors.bg_card};"
                f" color: {_theme.colors.text_primary};"
                f" border: 1px solid {_theme.colors.border}; border-radius: 10px;"
                f" margin-top: 10px; padding-top: 8px; }}"
                f"\nQDialog QGroupBox::title {{ subcontrol-origin: margin;"
                f" color: {_theme.colors.text_primary};"
                f" left: 12px; padding: 0 6px; }}"
            )
            dlg.setStyleSheet(_dlg_qss)
        # 主题颜色快捷变量（避免内联 setStyleSheet 使用硬编码颜色）
        _c = _theme.colors if _theme else None
        CLR_SEC = _c.text_secondary if _c else "#9599A6"
        CLR_MUTE = _c.text_muted if _c else "#666B75"
        CLR_BG2 = _c.bg_secondary if _c else "#222427"
        CLR_WARN = _c.warning if _c else "#D27E24"
        CLR_ERR = _c.error if _c else "#F65A5A"
        CLR_OK = _c.success if _c else "#32F08C"
        CLR_INFO = _c.accent_secondary if _c else "#7BB8FF"

        form = QVBoxLayout(dlg)
        form.setContentsMargins(20, 20, 20, 20)
        form.setSpacing(16)

        # 1. DSH 连接
        grp_conn = QGroupBox("DSH 连接")
        conn_layout = QFormLayout(grp_conn)
        conn_layout.setSpacing(10)
        endpoint_edit = QLineEdit(self.config.custom_dsh_endpoint or C.DSH_BASE_URL)
        endpoint_edit.setPlaceholderText(f"默认：{C.DSH_BASE_URL}")
        conn_layout.addRow("自定义 DSH 端点：", endpoint_edit)
        hint = QLabel("留空则使用默认 http://127.0.0.1:3080。")
        hint.setStyleSheet(f"color:{CLR_SEC}; font-size:11px;")
        hint.setWordWrap(True)
        conn_layout.addRow(hint)
        form.addWidget(grp_conn)

        # 2. API Key
        grp_key = QGroupBox("DeepSeek API 密钥")
        key_layout = QFormLayout(grp_key)
        key_layout.setSpacing(10)
        import os, yaml
        dsh_home = os.path.join(os.path.expanduser("~"), ".dsh")
        creds_file = os.path.join(dsh_home, ".credentials.yaml")
        current_key = ""
        try:
            if os.path.isfile(creds_file):
                with open(creds_file, "r", encoding="utf-8") as f:
                    creds_data = yaml.safe_load(f) or {}
                current_key = str(creds_data.get("DEEPSEEK_API_KEY", ""))
        except Exception:
            pass
        key_edit = QLineEdit(current_key)
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText("sk-...")
        key_layout.addRow("API 密钥：", key_edit)
        show_key_cb = QCheckBox("显示明文")
        def toggle_key_visibility(checked: bool):
            key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        show_key_cb.toggled.connect(toggle_key_visibility)
        key_layout.addRow(show_key_cb)

        # 验证按钮 + 状态标签（实时校验 Key 是否有效）
        verify_row = QHBoxLayout()
        verify_btn = QPushButton("验证 Key")
        verify_status_lbl = QLabel("尚未验证")
        verify_status_lbl.setStyleSheet(f"color:{CLR_SEC}; font-size:11px;")
        def do_verify():
            key_to_check = key_edit.text().strip()
            if not key_to_check:
                verify_status_lbl.setText("⚠ 请先输入 Key")
                verify_status_lbl.setStyleSheet(f"color:{CLR_WARN}; font-size:11px;")
                return
            # 临时写入 creds 文件，让 DSH 热加载后校验
            try:
                os.makedirs(dsh_home, exist_ok=True)
                import yaml as _yaml_v
                v_data = {}
                if os.path.isfile(creds_file):
                    with open(creds_file, "r", encoding="utf-8") as f:
                        v_data = _yaml_v.safe_load(f) or {}
                v_data["DEEPSEEK_API_KEY"] = key_to_check
                with open(creds_file, "w", encoding="utf-8") as f:
                    _yaml_v.dump(v_data, f, default_flow_style=False, allow_unicode=True)
            except Exception as ev:
                verify_status_lbl.setText("⚠ 写入失败: %s" % ev)
                verify_status_lbl.setStyleSheet(f"color:{CLR_ERR}; font-size:11px;")
                return
            verify_status_lbl.setText("验证中...")
            verify_status_lbl.setStyleSheet(f"color:{CLR_INFO}; font-size:11px;")
            QApplication.processEvents()
            try:
                creds = self.dsh.check_credentials() or {}
                if creds.get("valid"):
                    provider = creds.get("provider", "")
                    extra = f" ({provider})" if provider else ""
                    verify_status_lbl.setText("✓ Key 有效%s" % extra)
                    verify_status_lbl.setStyleSheet(f"color:{CLR_OK}; font-size:11px;")
                elif creds.get("configured"):
                    verify_status_lbl.setText("⚠ Key 已配置但校验未通过")
                    verify_status_lbl.setStyleSheet(f"color:{CLR_WARN}; font-size:11px;")
                else:
                    verify_status_lbl.setText("⚠ DSH 未识别到 Key")
                    verify_status_lbl.setStyleSheet(f"color:{CLR_WARN}; font-size:11px;")
            except Exception as ev:
                verify_status_lbl.setText("⚠ 验证失败: %s" % ev)
                verify_status_lbl.setStyleSheet(f"color:{CLR_ERR}; font-size:11px;")
        verify_btn.clicked.connect(do_verify)
        verify_row.addWidget(verify_btn)
        verify_row.addWidget(verify_status_lbl, stretch=1)
        verify_wrap = QWidget(grp_key)
        verify_wrap.setLayout(verify_row)
        key_layout.addRow(verify_wrap)

        key_hint = QLabel(
            f"密钥存储在 {creds_file}\nDSH 会自动热加载，保存后立即生效。"
        )
        key_hint.setStyleSheet(f"color:{CLR_SEC}; font-size:11px;")
        key_hint.setWordWrap(True)
        key_layout.addRow(key_hint)
        form.addWidget(grp_key)

        # 3. 主题（tm 已在对话框创建时加载，此处复用）
        grp_theme = QGroupBox("主题")
        theme_layout = QFormLayout(grp_theme)
        theme_combo = QComboBox()
        theme_keys_list: list[str] = []
        for key_name, display_name in tm.theme_keys.items():
            cn_name = tm.cn_display_name(key_name)
            theme_combo.addItem(cn_name, key_name)
            theme_keys_list.append(key_name)
            if key_name == self.config.theme:
                theme_combo.setCurrentIndex(theme_combo.count() - 1)
        theme_layout.addRow("颜色主题：", theme_combo)
        form.addWidget(grp_theme)

        # 4. 工作区
        grp_ws = QGroupBox("工作区")
        ws_layout = QHBoxLayout()
        ws_edit = QLineEdit(self.config.workspace)
        ws_edit.setPlaceholderText("未设置")
        ws_btn = QPushButton("选择...")
        def pick_ws():
            from PySide6.QtWidgets import QFileDialog
            d = QFileDialog.getExistingDirectory(dlg, "选择工作区目录")
            if d:
                ws_edit.setText(d)
        ws_btn.clicked.connect(pick_ws)
        ws_layout.addWidget(ws_edit, stretch=1)
        ws_layout.addWidget(ws_btn)
        ws_wrap = QWidget(grp_ws)
        ws_wrap.setLayout(ws_layout)
        ws_form = QFormLayout(grp_ws)
        ws_form.addRow("默认工作区：", ws_wrap)
        form.addWidget(grp_ws)

        # 5. 行为
        grp_behave = QGroupBox("行为")
        behave_layout = QFormLayout(grp_behave)
        tray_cb = QCheckBox("关闭窗口时最小化到系统托盘（推荐）")
        tray_cb.setChecked(bool(self.config.minimize_to_tray))
        behave_layout.addRow(tray_cb)
        readability_cb = QCheckBox("可读性自动保护")
        readability_cb.setChecked(bool(self.config.readability_protection))
        behave_layout.addRow(readability_cb)
        balance_label_cb = QCheckBox("余额标注来源")
        balance_label_cb.setChecked(bool(self.config.balance_source_label))
        behave_layout.addRow(balance_label_cb)
        form.addWidget(grp_behave)

        # 6. 快捷键说明
        grp_keys = QGroupBox("快捷键")
        keys_layout = QVBoxLayout(grp_keys)
        keys_txt = QPlainTextEdit()
        keys_txt.setReadOnly(True)
        keys_txt.setFixedHeight(110)
        keys_txt.setPlainText(
            "Ctrl+N          新建会话\n"
            "Ctrl+Shift+F    搜索\n"
            "Ctrl+B          切换左栏显示\n"
            "Ctrl+J          切换右栏显示\n"
            "Ctrl+Shift+M    切换 Work / Code 模式\n"
            "Ctrl+.          循环切换主题\n"
            "Ctrl+Enter      发送消息\n"
            "Esc             停止 Agent\n"
            "Ctrl+,          打开设置"
        )
        keys_txt.setStyleSheet(
            f"background-color:{CLR_BG2}; color:{CLR_SEC}; "
            "font-family: Consolas, monospace; font-size:11px;"
        )
        keys_layout.addWidget(keys_txt)
        form.addWidget(grp_keys)

        # 按钮
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        # 中文化按钮文字
        _save_btn = btns.button(QDialogButtonBox.StandardButton.Save)
        _cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        if _save_btn:
            _save_btn.setText("保存")
        if _cancel_btn:
            _cancel_btn.setText("取消")
        def accept_all():
            ep = endpoint_edit.text().strip()
            self.config.custom_dsh_endpoint = ep if ep and ep != C.DSH_BASE_URL else ""
            new_key = key_edit.text().strip()
            if new_key != current_key:
                try:
                    os.makedirs(dsh_home, exist_ok=True)
                    import yaml as _yaml
                    creds_data = {}
                    if os.path.isfile(creds_file):
                        with open(creds_file, "r", encoding="utf-8") as f:
                            creds_data = _yaml.safe_load(f) or {}
                    if new_key:
                        creds_data["DEEPSEEK_API_KEY"] = new_key
                    else:
                        creds_data.pop("DEEPSEEK_API_KEY", None)
                    with open(creds_file, "w", encoding="utf-8") as f:
                        _yaml.dump(creds_data, f, default_flow_style=False, allow_unicode=True)
                except Exception as e:
                    log.error("保存 API Key 失败: %s", e)
            new_theme_key = theme_combo.currentData()
            if new_theme_key and new_theme_key in theme_keys_list:
                self.config.theme = new_theme_key
                theme = tm.set_current(new_theme_key)
                if theme:
                    qss = tm.generate_qss(theme)
                    QApplication.instance().setStyleSheet(qss)
            ws = ws_edit.text().strip()
            if ws != self.config.workspace:
                self.config.workspace = ws
                self.title_bar.set_workspace(ws)
                self.left_panel.load_workspace(ws)
            self.config.minimize_to_tray = tray_cb.isChecked()
            self.config.readability_protection = readability_cb.isChecked()
            self.config.balance_source_label = balance_label_cb.isChecked()
            if not self.config.minimize_to_tray:
                try:
                    self.system_tray.hide()
                except Exception:
                    pass
            else:
                try:
                    self.system_tray.setup()
                except Exception:
                    pass
            self.config.save()
            log.info("设置已保存")
            dlg.accept()
        btns.accepted.connect(accept_all)
        btns.rejected.connect(dlg.reject)
        form.addWidget(btns)
        dlg.exec()

    def _action_change_workspace(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        workspace = QFileDialog.getExistingDirectory(self, "选择工作区")
        if workspace:
            self.config.workspace = workspace
            self.config.save()
            self.title_bar.set_workspace(workspace)
            self.left_panel.load_workspace(workspace)

    def _on_scrolled_to_top(self) -> None:
        if self.session_manager.current_session_id:
            self.session_manager.load_more_history(
                self.session_manager.current_session_id
            )

    def _on_restore(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def notify_task_completed(self, title: str, body: str) -> None:
        """SessionWatcher 回调：Agent 任务完成时弹系统通知。

        入口由 app.py 把 core.session_watcher.TurnEndEvent 翻译后调用。
        """
        if not title or not body:
            return
        try:
            if self.system_tray is not None:
                self.system_tray.notify(title, body)
        except Exception as e:
            log.warning("任务完成通知发送失败: %s", e)

    # ===== 窗口关闭 =====

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.config.minimize_to_tray and self.system_tray:
            event.ignore()
            self.hide()
            self.system_tray.notify("DSH Work", "已最小化到系统托盘")
        else:
            self._on_quit()
            event.accept()

    def _on_quit(self) -> None:
        if getattr(self, "_quit_triggered", False):
            return
        self._quit_triggered = True
        log.info("DSH Work 正在退出...")
        try:
            if self.dsh is not None:
                self.dsh.shutdown()
        except Exception as e:
            log.error("关闭 DSH 服务失败: %s", e)
        try:
            if self.system_tray is not None:
                self.system_tray.hide()
        except Exception:
            pass
        self.quit_requested.emit()
        try:
            QApplication.instance().quit()
        except Exception:
            pass

    # ===== 运行时状态更新 =====

    def update_runtime_status(self) -> None:
        def on_balance(result):
            self.status_bar.set_balance(result)
            # 同时刷新内联小部件的余额
            QTimer.singleShot(0, lambda: self.center_panel.set_balance(result))
        self.dsh.balance.query_async(on_balance)

    def _query_balance_async(self, force: bool = False) -> None:
        """后台异步查询余额，结果同时更新状态栏与内联小部件。

        回调在 worker 线程执行，通过 QTimer.singleShot(0, ...) 把 UI 更新
        投递回主线程（避免跨线程直接操作 Qt widget）。
        """
        if self.dsh is None or self.dsh.balance is None:
            return
        try:
            def on_result(result):
                # 跨线程安全：QTimer.singleShot(0, cb) 把 cb 放到主线程事件队列
                def apply_ui():
                    self.status_bar.set_balance(result)
                    self.center_panel.set_balance(result)
                QTimer.singleShot(0, apply_ui)
            self.dsh.balance.query_async(on_result, force=force)
        except Exception as e:
            log.warning("触发余额异步查询失败: %s", e)

    def _action_refresh_balance(self) -> None:
        """用户点击余额小部件触发的强制刷新。"""
        self.center_panel.set_balance_loading()
        self.status_bar.show_temporary("🔄 正在刷新余额…", color="#387BFF", duration_ms=1500)
        # force=True 跳过 5 分钟缓存
        self._query_balance_async(force=True)

    def refresh_models(self) -> None:
        try:
            models = self.dsh.get_models()
            model_ids = [m.id for m in models]
            if not model_ids:
                if self.dsh.is_offline:
                    self.title_bar.set_models(
                        ["[离线] DSH 未连接"], self.config.last_model
                    )
                    self.title_bar._model_combo.setEnabled(False)
                elif self.dsh.compatibility_mode == CompatibilityMode.DEGRADED:
                    self.title_bar.set_models(
                        ["[降级] 协议不匹配"], self.config.last_model
                    )
                    self.title_bar._model_combo.setEnabled(False)
                else:
                    self.title_bar.set_models(
                        ["[无可用模型] 请检查 API Key"], self.config.last_model
                    )
                    self.title_bar._model_combo.setEnabled(False)
            else:
                self.title_bar._model_combo.setEnabled(True)
                self.title_bar.set_models(model_ids, self.config.last_model)
        except Exception as e:
            log.warning("刷新模型列表失败: %s", e)
            self.title_bar.set_models(["[错误] 刷新失败"], "")
            self.title_bar._model_combo.setEnabled(False)

    def refresh_sessions(self) -> None:
        try:
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                infos = loop.run_until_complete(self.session_manager.refresh_sessions())
                self.left_panel.refresh_sessions(infos)
            finally:
                loop.close()
        except Exception as e:
            log.warning("刷新会话列表失败: %s", e)

    def set_dsh_status(self, mode: CompatibilityMode, version: str) -> None:
        self.status_bar.set_connection_status(mode)
        self.status_bar.set_dsh_version(version)
        self.system_tray.set_connection_status(mode != CompatibilityMode.OFFLINE)
