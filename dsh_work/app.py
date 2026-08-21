"""应用初始化与启动流程编排（第 7.1 节）。

启动流程四阶段：
1. 环境检测（后台自动，3-8秒）
2. 场景选择（用户交互，10秒内）
3. 自动配置（后台，1-2秒）
4. 开始工作（主窗口）

高 DPI 适配必须在 QApplication 实例化之前调用（第 8.8 节）。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from . import constants as C
from .api import CompatibilityMode, DshService
from .config import UserConfig, ensure_dirs, get_dsh_sessions_dir
from .core.process_manager import ProcessManager
from .core.session_watcher import SessionWatcher, TurnEndEvent
from .core.update_checker import UpdateInfo, check_for_updates
from .ui.main_window import MainWindow
from .ui.onboarding.scenario_picker import ScenarioPicker
from .ui.onboarding.splash_screen import SplashScreen
from .ui.theme.theme_manager import ThemeManager
from .utils.high_dpi import setup_high_dpi
from .utils.logger import get_logger

log = get_logger("app")


class _UpdateEmitter(QObject):
    """更新检查信号桥：daemon 线程 emit → 主线程 slot（Qt 自动队列化跨线程连接）。"""

    update_found = Signal(object)


class _TaskDoneEmitter(QObject):
    """会话完成通知信号桥：SessionWatcher 后台线程 emit → 主线程 slot。

    SessionWatcher 的 scan() 跑在自己的 daemon 线程里，不能直接调
    MainWindow.notify_task_completed（Qt UI 方法必须在主线程）。
    """

    task_done = Signal(str, str)  # (title, body)


class DshWorkApp:
    """DSH Work 应用编排器。

    负责：
    1. 高 DPI 设置 + QApplication 创建
    2. 环境检测（启动画面）
    3. 首次启动场景选择引导
    4. 主题加载
    5. DSH 服务初始化
    6. 主窗口显示 + 会话日志监控（任务完成通知）
    7. 退出清理（DSH 进程生命周期管理 + 监控线程停止）
    """

    def __init__(self):
        # 高 DPI 必须在 QApplication 之前
        setup_high_dpi()
        self.qt_app = QApplication.instance() or QApplication([])
        self.qt_app.setApplicationName(C.APP_NAME)
        self.qt_app.setOrganizationName(C.ORG_NAME)
        self.qt_app.setApplicationVersion(C.APP_VERSION)

        # 确保目录存在
        ensure_dirs()

        # 配置
        self.config = UserConfig.load()

        # 核心组件
        self.process_manager = ProcessManager()
        self.dsh: DshService | None = None
        self.main_window: MainWindow | None = None
        self.theme_manager = ThemeManager()
        self.session_watcher: SessionWatcher | None = None

        # 退出保护：登记在 aboutToQuit 里再做一次 cleanup，
        # 保证在 QWidget/QObject C++ 析构开始前就把 QThread/子进程都停干净。
        self._cleanup_done = False
        try:
            # aboutToQuit 是 Qt 发出的信号，此时主事件循环仍在运行，
            # QObject 仍然存活，quit/wait 能被正确派发。
            self.qt_app.aboutToQuit.connect(self.cleanup)
        except Exception:
            pass

    def run(self) -> int:
        """启动应用主循环。"""
        log.info("DSH Work 启动中 v%s", C.APP_VERSION)

        # Step 1: 环境检测
        self._run_environment_check()

        # 若环境检测未通过且用户未关闭，继续进入主窗口（离线模式）
        try:
            if not self._init_dsh_service():
                log.warning("DSH 服务不可用，进入离线模式")
        except Exception:
            log.exception("DSH 服务初始化阶段抛异常，进入离线模式")
            self.dsh = None

        # Step 2: 加载主题
        try:
            self._load_theme()
        except Exception:
            log.exception("主题加载失败（不致命，继续启动）")

        # Step 3: 首次启动场景选择
        if self.config.first_run:
            try:
                self._show_scenario_picker()
            except Exception:
                log.exception("场景选择引导异常（跳过）")
            self.config.first_run = False
            self.config.save()

        # Step 4: 显示主窗口
        try:
            self._show_main_window()
        except Exception:
            log.exception("主窗口显示失败，尝试弹出错误对话框")
            try:
                from PySide6.QtWidgets import QMessageBox
                msg = QMessageBox()
                msg.setIcon(QMessageBox.Icon.Critical)
                msg.setWindowTitle("DSH Work 启动失败")
                msg.setText("主窗口初始化失败，请查看日志。")
                msg.setDetailedText("请将日志文件发送给开发者以定位问题。\n日志目录：见 utils.logger 配置。")
                msg.exec()
            except Exception:
                log.exception("错误对话框也失败了")
            return 1

        return self.qt_app.exec()

    def _run_environment_check(self) -> None:
        """Step 1: 环境检测（启动画面）。

        环境检测跑在 daemon threading.Thread 里（不再是 QThread），
        进程退出时自动终止，不会触发 Destroyed 告警。
        这里仍保存 worker 引用，以便在退出时 abort 让它尽快收尾。
        """
        splash = SplashScreen(self.process_manager, workspace=self.config.workspace)
        self._splash_worker = splash.worker
        try:
            splash.exec()
        except KeyboardInterrupt:
            log.warning("用户中断启动画面（Ctrl+C），等待环境检测线程自然结束...")
            # KeyboardInterrupt 在 Qt 事件循环中可能被捕获，但为了安全起见，
            # 我们也在这里捕获，确保不会因为中断而跳过环境检测结果读取
        self._env_check = splash.check_result

        if self._env_check and not self._env_check.all_ok:
            log.warning("环境检测发现问题: %s", self._env_check.errors)
            if self._env_check.dsh_running:
                log.info("DSH 可用，继续启动")
            else:
                log.warning("DSH 不可用，将进入离线模式")

    def _init_dsh_service(self) -> bool:
        """初始化 DSH 通信服务。"""
        if not self._env_check or not self._env_check.dsh_running:
            return False

        try:
            self.dsh = DshService()
            probe = self.dsh.initialize()
            if probe.success:
                log.info("DSH 服务初始化成功: version=%s", probe.version)
                self.dsh.start_websocket()
                return True
            else:
                log.warning("DSH 版本适配器探测失败: %s，进入%s模式",
                            probe.error, probe.mode.value)
                # 兼容降级模式仍可使用
                if probe.mode == CompatibilityMode.DEGRADED:
                    self.dsh.start_websocket()
                    return True
                return False
        except Exception as e:
            log.error("DSH 服务初始化异常: %s", e)
            self.dsh = None
            return False

    def _load_theme(self) -> None:
        """加载并应用主题。"""
        self.theme_manager.load_all()
        theme = self.theme_manager.set_current(self.config.theme)
        if theme:
            qss = self.theme_manager.generate_qss(theme)
            self.qt_app.setStyleSheet(qss)
            log.info("主题已应用: %s", theme.name)

    def _show_scenario_picker(self) -> None:
        """首次启动场景选择引导。"""
        picker = ScenarioPicker()

        def on_selected(prompt: str, mode: str):
            self.config.mode = mode
            self.config.last_scenario = prompt
            self.config.save()
            log.info("场景选择: mode=%s prompt=%s...", mode, prompt[:30])

        picker.scenario_selected.connect(on_selected)
        picker.exec()

    def _show_main_window(self) -> None:
        """Step 4: 显示主窗口。"""
        if not self.dsh:
            # 离线模式：创建一个最小的 DshService 用于历史缓存读取
            self.dsh = DshService()

        self.main_window = MainWindow(self.dsh)

        # 应用初始配置（preset 切换由 MainWindow._apply_config 内部处理）
        self.main_window.status_bar.set_mode(self.config.mode)

        # 设置 DSH 状态
        if self.dsh:
            mode = self.dsh.compatibility_mode
            version = self.dsh.dsh_version
            self.main_window.set_dsh_status(mode, version)

        # 刷新模型与会话
        if self.dsh and self.dsh.is_full_mode:
            QTimer.singleShot(100, self.main_window.refresh_models)
            QTimer.singleShot(200, self.main_window.refresh_sessions)
            # 延迟 500ms 恢复上次的对话缓存（等待 refresh_sessions 完成）
            QTimer.singleShot(500, self.main_window._load_conversation_state)

        # 定时刷新运行时状态（余额等）
        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self.main_window.update_runtime_status)
        self._status_timer.start(C.BALANCE_REFRESH_INTERVAL_SEC * 1000)
        # 启动后 3 秒首次查询余额（DSH 服务可能刚就绪，避免启动时就查询失败）
        QTimer.singleShot(3000, self.main_window.update_runtime_status)

        self.main_window.show()
        log.info("主窗口已显示")

        # 启动会话日志监控（任务完成 → 系统通知，SessionWatcher P0改进）
        self._start_session_watcher()

        # 启动后延迟检查更新（后台线程，不阻塞 UI）
        self._schedule_update_check()

    def _start_session_watcher(self) -> None:
        """启动会话日志监控：DSH 任务完成时通过托盘弹出系统通知。

        设计对齐 dsh_desktop session-watcher.js：
          · 后台线程扫描 ~/.dsh/sessions/**/session.jsonl.zstd
          · 基线化（启动时不弹历史 toast，只对后续新增的 turn/end 触发）
          · 增量扫描（每 3s 一次，目录枚举缓存 5s，首扫分批限流）
          · 通过 _TaskDoneEmitter 信号桥跨线程安全回调 UI

        若用户关闭了系统托盘（minimize_to_tray=False）则跳过 ——
        没有托盘图标时 QSystemTrayIcon.showMessage 在部分平台会被吞。
        """
        if not getattr(self.config, "minimize_to_tray", True):
            return
        if self.main_window is None:
            return

        # 信号桥：SessionWatcher 后台线程 → 主线程 UI
        self._task_done_emitter = _TaskDoneEmitter()
        self._task_done_emitter.task_done.connect(self.main_window.notify_task_completed)

        def on_turn_end(ev: TurnEndEvent) -> None:
            # daemon 线程里只能 emit，绝不能直接调 Qt UI 方法
            try:
                self._task_done_emitter.task_done.emit(ev.title, ev.body)
            except Exception:
                pass

        try:
            sessions_dir = get_dsh_sessions_dir()
            self.session_watcher = SessionWatcher(
                sessions_dir=sessions_dir,
                on_turn_end=on_turn_end,
            )
            self.session_watcher.start(interval_sec=3.0)
        except Exception as e:
            log.warning("启动会话日志监控失败(不致命): %s", e)
            self.session_watcher = None

    def _schedule_update_check(self) -> None:
        """启动后延迟在后台线程检查更新，有新版则回主线程弹窗提示（P3-4）。

        非阻塞：检查跑在 daemon 线程，结果通过信号回到主线程显示对话框。
        用户关闭更新检查（config.check_updates=False）或无 UPDATE_CHECK_URL 时跳过。
        """
        if not getattr(self.config, "check_updates", True):
            return
        if not C.UPDATE_CHECK_URL:
            return

        # 信号桥：daemon 线程 emit → 主线程 slot（Qt 自动队列化跨线程连接）
        self._update_emitter = _UpdateEmitter()
        self._update_emitter.update_found.connect(self._on_update_found)

        def _worker():
            info = check_for_updates()
            if info and info.latest_version != getattr(self.config, "skip_update_version", ""):
                # 跨线程 emit：Qt 把调用排入主线程事件队列
                self._update_emitter.update_found.emit(info)

        import threading

        def _start():
            t = threading.Thread(target=_worker, name="update-checker", daemon=True)
            t.start()

        QTimer.singleShot(C.UPDATE_CHECK_DELAY_MS, _start)

    def _on_update_found(self, info: UpdateInfo) -> None:
        """主线程槽：发现新版本时弹出提示对话框。"""
        if self.main_window is None:
            return
        log.info(
            "更新可用: %s -> %s", info.current_version, info.latest_version
        )
        notes = (info.release_notes or "").strip()
        notes_line = f"\n\n更新说明：\n{notes[:500]}" if notes else ""
        box = QMessageBox(self.main_window)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("发现新版本")
        box.setText(
            f"DSH Work 有新版本可用。\n"
            f"当前版本：{info.current_version}\n"
            f"最新版本：{info.latest_version}{notes_line}"
        )
        btn_download = box.addButton("前往下载", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("暂不更新", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is btn_download and info.download_url:
            QDesktopServices.openUrl(QUrl(info.download_url))
        elif box.clickedButton() is not btn_download:
            # 记录跳过版本，避免反复弹窗打扰
            self.config.skip_update_version = info.latest_version
            self.config.save()

    def cleanup(self) -> None:
        """退出清理：DSH 进程生命周期管理 + daemon 线程收尾。

        环境检测 worker 使用 daemon threading.Thread，进程退出时自动终止，
        不需要也不应该 wait（wait 会导致退出卡顿）。这里只做 abort 让它
        尽快结束，然后处理 DshService.shutdown 和 ProcessManager.stop_dsh。

        aboutToQuit 与 main() 的 finally 都会调用这里，用 _cleanup_done 防重。
        """
        if getattr(self, "_cleanup_done", False):
            return
        self._cleanup_done = True

        log.info("应用退出清理中...")

        # 0) 停会话日志监控线程（避免在 QObject 析构阶段仍在回调信号）
        sw = getattr(self, "session_watcher", None)
        if sw is not None:
            try:
                sw.stop()
            except Exception as e:
                log.error("停止 SessionWatcher 失败: %s", e)
            self.session_watcher = None

        # 1) 停状态刷新定时器
        status_timer = getattr(self, "_status_timer", None)
        if status_timer is not None:
            try:
                status_timer.stop()
            except Exception:
                pass

        # 2) abort 环境检测 daemon 线程（不 wait，让它自然退出）
        splash_worker = getattr(self, "_splash_worker", None)
        if splash_worker is not None:
            try:
                splash_worker.abort()
            except Exception:
                pass

        # 3) 主窗口退出清理
        if self.main_window is not None:
            try:
                if not getattr(self.main_window, "_quit_triggered", False):
                    self.main_window._quit_triggered = True
                    try:
                        if self.main_window.system_tray is not None:
                            self.main_window.system_tray.hide()
                    except Exception:
                        pass
                    try:
                        if self.main_window.dsh is not None:
                            self.main_window.dsh.shutdown()
                    except Exception as e:
                        log.error("关闭 DSH 服务失败: %s", e)
                    try:
                        self.main_window.quit_requested.emit()
                    except Exception:
                        pass
            except Exception as e:
                log.error("主窗口退出清理异常: %s", e)

        # 4) DSH 子进程终止 + join 日志透传线程
        try:
            if self.process_manager.is_dsh_owned:
                self.process_manager.stop_dsh()
        except Exception as e:
            log.error("DSH 进程终止异常: %s", e)

        log.info("应用退出清理完成")
