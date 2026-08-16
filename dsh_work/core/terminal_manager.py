"""会话内 PowerShell SSE 流式终端。

目标：每个 DSH 会话都可绑定一个独立的 PowerShell（pwsh / Windows PowerShell）子进程，
UI 通过回调拿到 SSE 风格的流式输出（event: output / event: exit / event: prompt）。

关键特性：
  - 中文输出不乱码：启动 PowerShell 时 chcp 65001，子进程 stdout/stderr 统一 utf-8
  - 命令历史：每条 write_line 命令追加到环形 history（跨重启保留），持久化到磁盘
  - 断线重连：进程意外退出时，若 auto_reconnect=True，延时后自动重启，恢复上次 CWD
  - 输入隔离：stdin 是线程安全队列，UI 和后端线程都可以写入
  - 输出节流：SSE 回调以 30ms 窗口合并，输出暴增时避免 UI 逐字刷新卡顿
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

from ..paths import Paths
from ..utils.logger import get_logger

log = get_logger("core.terminal")

_OUTPUT_FLUSH_MS = 30
_MAX_HISTORY = 500
_AUTO_RECONNECT_DELAY_SEC = 1.0

# 常见 ANSI 转义序列，默认保留颜色（UI 渲染层自己处理）；
# strip_ansi=True 时使用此正则剥离（纯文本场景）
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class TerminalLine:
    kind: str       # "stdout" / "stderr" / "meta" / "input"
    text: str
    timestamp_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TerminalEvent:
    """SSE 风格事件（直接给 UI 层 consume）。"""
    event: str      # "output" / "exit" / "started" / "prompt"
    data: str = ""
    timestamp_ms: int = 0

    def to_sse(self) -> str:
        data = self.data.replace("\r", "")
        lines = [f"event: {self.event}"]
        for ln in data.split("\n"):
            lines.append(f"data: {ln}")
        return "\n".join(lines) + "\n\n"


class PowerShellSession:
    """单个会话的 PowerShell 子进程管理。"""

    def __init__(
        self,
        session_id: str,
        cwd: str | Path | None = None,
        *,
        auto_reconnect: bool = True,
        strip_ansi: bool = False,
        on_event: Callable[[TerminalEvent], None] | None = None,
    ):
        self.session_id = session_id
        self.cwd = str(Path(cwd).expanduser()) if cwd else str(Path.cwd())
        self.auto_reconnect = auto_reconnect
        self.strip_ansi = strip_ansi
        self.on_event = on_event

        self._proc: subprocess.Popen[str] | None = None
        self._stdin_queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.RLock()

        # 命令历史：持久化
        self._history: list[str] = []
        self._history_cursor: int = 0
        self._history_path = Paths.user_data() / "terminal_history" / f"{self._safe(session_id)}.json"
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_history()

        # 输出节流（30ms 合并一次 TerminalEvent output）
        self._buf: list[TerminalLine] = []
        self._buf_lock = threading.Lock()
        self._buf_timer: threading.Timer | None = None

        # 守护线程
        self._exit_watcher: threading.Thread | None = None
        self._stdin_writer: threading.Thread | None = None
        self._stdout_reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._reconnect_waiter: threading.Thread | None = None
        self._stopping = False

    # =================================================================
    #  启动 / 关闭
    # =================================================================

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._stopping = False
            exe = self._pick_pwsh_exe()
            if exe is None:
                self._emit_meta("[终端启动失败] 未找到 pwsh 或 powershell.exe。")
                return
            env = os.environ.copy()
            # 强制 UTF-8 输出
            env["PYTHONIOENCODING"] = "utf-8"
            args = [
                exe,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                # 进入后先切 UTF-8 代码页，再还原 cwd
                "-Command",
                # 注意：以下是 PowerShell -Command 的一次执行；为了保持交互会话，
                # 我们改用 stdin 模式：启动 powershell -NoProfile -NoLogo 不执行 -Command，
                # 初始化命令通过 stdin 注入。所以这里 args 实际上要重写，见下。
            ]
            # 覆盖上面：用交互模式 powershell，初始化通过 stdin 发
            args = [exe, "-NoLogo", "-NoProfile"]
            try:
                proc = subprocess.Popen(
                    args,
                    cwd=self.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=0,
                )
            except OSError as e:
                self._emit_meta(f"[终端启动失败] {e}")
                return
            self._proc = proc
            # 启动初始化命令：chcp 65001；Set-Location 到 cwd；$PROFILE 提示美化（可选）
            init_cmds = [
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
                "$OutputEncoding = [System.Text.Encoding]::UTF8",
                "chcp 65001 > $null",
                f"Set-Location -LiteralPath '{self._escape_ps(self.cwd)}'",
            ]
            for cmd in init_cmds:
                self._stdin_queue.put(cmd + "\n")
            # 输出一个标记串，UI 可以据此知道启动完成
            self._stdin_queue.put("Write-Host \"[dsh-work] terminal-ready\"" + "\n")

            self._stdin_writer = threading.Thread(target=self._stdin_writer_loop, daemon=True,
                                                   name=f"ps-stdin:{session_id}")
            self._stdout_reader = threading.Thread(target=self._reader_loop, args=("stdout",),
                                                   daemon=True, name=f"ps-stdout:{session_id}")
            self._stderr_reader = threading.Thread(target=self._reader_loop, args=("stderr",),
                                                   daemon=True, name=f"ps-stderr:{session_id}")
            self._exit_watcher = threading.Thread(target=self._exit_watcher_loop, daemon=True,
                                                  name=f"ps-watch:{session_id}")
            self._stdin_writer.start()
            self._stdout_reader.start()
            self._stderr_reader.start()
            self._exit_watcher.start()
            log.info("PowerShell 已启动 session=%s exe=%s cwd=%s", session_id, exe, self.cwd)
            self._emit(TerminalEvent(event="started",
                                     data=json.dumps({"exe": exe, "cwd": self.cwd},
                                                     ensure_ascii=False)))

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            if self._buf_timer is not None:
                try:
                    self._buf_timer.cancel()
                except Exception:
                    pass
                self._buf_timer = None
            proc = self._proc
            self._proc = None
        # 先尝试写 exit
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.write("exit\n")
                proc.stdin.flush()
            except Exception:
                pass
            # 给 1s 优雅退出
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._flush_buffer(force=True)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    # =================================================================
    #  命令输入 / 历史
    # =================================================================

    def write_line(self, line: str) -> None:
        """往终端输入一条命令；同时追加到 history。"""
        if line is None:
            return
        # history 去重（避免把大量空串加入）
        text = line.rstrip("\n")
        if text.strip():
            with self._lock:
                if not self._history or self._history[-1] != text:
                    self._history.append(text)
                    if len(self._history) > _MAX_HISTORY:
                        self._history = self._history[-_MAX_HISTORY:]
                    self._save_history_locked()
                self._history_cursor = len(self._history)
            # UI 回显输入（event: input 行）
            self._append_line(TerminalLine(kind="input", text=text + "\n"))
        self._stdin_queue.put((text + "\n"))

    def history_up(self) -> str | None:
        with self._lock:
            if not self._history:
                return None
            self._history_cursor = max(0, self._history_cursor - 1)
            return self._history[self._history_cursor] if self._history_cursor < len(self._history) else None

    def history_down(self) -> str | None:
        with self._lock:
            if not self._history:
                return None
            self._history_cursor = min(len(self._history), self._history_cursor + 1)
            if self._history_cursor >= len(self._history):
                return ""
            return self._history[self._history_cursor]

    def history_list(self, limit: int = 100) -> list[str]:
        with self._lock:
            return list(self._history[-limit:])

    # =================================================================
    #  内部：线程循环
    # =================================================================

    def _stdin_writer_loop(self) -> None:
        while True:
            try:
                chunk = self._stdin_queue.get(timeout=0.2)
            except queue.Empty:
                with self._lock:
                    proc = self._proc
                if proc is None or proc.poll() is not None:
                    return
                continue
            with self._lock:
                proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            try:
                if proc.stdin is not None:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
            except (OSError, ValueError):
                return

    def _reader_loop(self, kind: str) -> None:
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        stream = proc.stdout if kind == "stdout" else proc.stderr
        if stream is None:
            return
        try:
            while True:
                try:
                    chunk = stream.read(4096)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                self._append_line(TerminalLine(kind=kind, text=chunk))
        finally:
            # 读线程退出前立即 flush（避免尾部数据被节流吃掉）
            self._flush_buffer(force=True)

    def _exit_watcher_loop(self) -> None:
        while True:
            with self._lock:
                proc = self._proc
            if proc is None:
                return
            try:
                rc = proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue
            # 退出
            log.info("PowerShell 已退出 session=%s rc=%s", self.session_id, rc)
            self._flush_buffer(force=True)
            self._emit(TerminalEvent(event="exit", data=str(rc)))
            # 尝试重连
            if self.auto_reconnect and not self._stopping:
                if self._reconnect_waiter is None or not self._reconnect_waiter.is_alive():
                    self._reconnect_waiter = threading.Thread(
                        target=self._delayed_reconnect, daemon=True,
                        name=f"ps-reconnect:{self.session_id}",
                    )
                    self._reconnect_waiter.start()
            return

    def _delayed_reconnect(self) -> None:
        time.sleep(_AUTO_RECONNECT_DELAY_SEC)
        if self._stopping:
            return
        try:
            self.start()
            self._emit_meta("[终端已自动重连]")
        except Exception as e:
            log.warning("自动重连失败: %s", e)
            self._emit_meta(f"[终端自动重连失败] {e}")

    # =================================================================
    #  输出缓冲 + 节流合并（模拟 SSE 事件推送）
    # =================================================================

    def _append_line(self, line: TerminalLine) -> None:
        line.timestamp_ms = int(time.time() * 1000)
        if self.strip_ansi:
            line.text = _ANSI_RE.sub("", line.text)
        with self._buf_lock:
            self._buf.append(line)
            if self._buf_timer is None:
                self._buf_timer = threading.Timer(_OUTPUT_FLUSH_MS / 1000.0,
                                                  self._flush_buffer, (True,))
                self._buf_timer.daemon = True
                self._buf_timer.start()

    def _flush_buffer(self, force: bool = False) -> None:
        # 防重入：取消后续定时器回调
        with self._buf_lock:
            buf = self._buf
            self._buf = []
            timer = self._buf_timer
            self._buf_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if not buf:
            return
        # 合并为一个 output 事件（保留不同 kind 的颜色标记 JSON，UI 自行着色）
        payload = json.dumps([ln.to_dict() for ln in buf], ensure_ascii=False)
        self._emit(TerminalEvent(event="output", data=payload,
                                 timestamp_ms=buf[-1].timestamp_ms))

    def _emit_meta(self, text: str) -> None:
        self._append_line(TerminalLine(kind="meta", text=text + "\n"))

    def _emit(self, ev: TerminalEvent) -> None:
        if not ev.timestamp_ms:
            ev.timestamp_ms = int(time.time() * 1000)
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(ev)
        except Exception as e:
            log.warning("终端 on_event 回调异常: %s", e)

    # =================================================================
    #  历史持久化
    # =================================================================

    def _load_history(self) -> None:
        try:
            if self._history_path.exists():
                data = json.loads(self._history_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._history = [str(x) for x in data][-_MAX_HISTORY:]
                    self._history_cursor = len(self._history)
        except (OSError, ValueError):
            self._history = []
            self._history_cursor = 0

    def _save_history_locked(self) -> None:
        try:
            self._history_path.write_text(
                json.dumps(self._history, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            log.warning("保存终端历史失败: %s", e)

    # =================================================================
    #  工具
    # =================================================================

    @staticmethod
    def _pick_pwsh_exe() -> str | None:
        """优先跨平台 pwsh，回退 Windows 自带 powershell。"""
        def exists(exe: str) -> bool:
            try:
                subprocess.run([exe, "--version"], capture_output=True, timeout=5,
                               check=False)
                return True
            except (OSError, subprocess.TimeoutExpired):
                return False
        for cand in ["pwsh", "powershell", "powershell.exe"]:
            if exists(cand):
                return cand
        return None

    @staticmethod
    def _escape_ps(s: str) -> str:
        return s.replace("'", "''")

    @staticmethod
    def _safe(s: str) -> str:
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in s) or "global"


class TerminalManager:
    """按 session_id 管理多个 PowerShell 会话。"""

    def __init__(self):
        self._sessions: dict[str, PowerShellSession] = {}
        self._lock = threading.RLock()
        self._event_handlers: list[Callable[[str, TerminalEvent], None]] = []

    def add_event_handler(self, cb: Callable[[str, TerminalEvent], None]) -> None:
        self._event_handlers.append(cb)

    def remove_event_handler(self, cb) -> None:
        try:
            self._event_handlers.remove(cb)
        except ValueError:
            pass

    def _dispatch(self, session_id: str, ev: TerminalEvent) -> None:
        for cb in list(self._event_handlers):
            try:
                cb(session_id, ev)
            except Exception as e:
                log.warning("TerminalManager event handler 异常: %s", e)

    # ---------------- API ----------------
    def get_or_create(self, session_id: str, cwd: str | Path | None = None,
                      auto_reconnect: bool = True,
                      strip_ansi: bool = False) -> PowerShellSession:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None:
                return sess
            sess = PowerShellSession(
                session_id,
                cwd=cwd,
                auto_reconnect=auto_reconnect,
                strip_ansi=strip_ansi,
                on_event=lambda ev, sid=session_id: self._dispatch(sid, ev),
            )
            self._sessions[session_id] = sess
            return sess

    def start(self, session_id: str, cwd: str | Path | None = None) -> None:
        self.get_or_create(session_id, cwd=cwd).start()

    def write_line(self, session_id: str, line: str) -> None:
        self.get_or_create(session_id).write_line(line)

    def history_up(self, session_id: str) -> str | None:
        with self._lock:
            s = self._sessions.get(session_id)
        return s.history_up() if s else None

    def history_down(self, session_id: str) -> str | None:
        with self._lock:
            s = self._sessions.get(session_id)
        return s.history_down() if s else None

    def stop(self, session_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(session_id, None)
        if s is not None:
            s.stop()

    def stop_all(self) -> None:
        with self._lock:
            items = list(self._sessions.values())
            self._sessions.clear()
        for s in items:
            s.stop()
