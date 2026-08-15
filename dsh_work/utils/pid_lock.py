"""PID 文件锁：DSH 进程所有权管理。

仅靠探测 127.0.0.1:3080 无法区分"本客户端启动的 DSH"与"用户手动启动的 DSH"。
客户端用 PID 文件锁明确进程所有权：

| PID 校验结果                  | 客户端行为                                   |
|-------------------------------|----------------------------------------------|
| 一致（本客户端启动）          | 获取生命周期管理权，退出时可安全终止 DSH     |
| 不一致（用户手动启动）        | 仅附加连接，无权杀进程，退出时保留 DSH 运行  |
| .dsh.pid 存在但进程已僵死     | 杀死僵尸进程，由当前客户端重新拉起新的 DSH   |
| 端口被占但无 PID 文件         | 提示用户，询问附加连接或另起端口             |
"""

from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .. import constants as C
from ..config import get_pid_file_path
from .logger import get_logger

log = get_logger("utils.pid_lock")


class PidLockError(Exception):
    """PID 锁操作异常。"""


@dataclass
class PidInfo:
    """PID 文件内容。"""

    pid: int
    port: int
    started_at: float
    owner: str = "dsh-work"  # 启动者标识

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "port": self.port,
            "started_at": self.started_at,
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PidInfo:
        return cls(
            pid=int(data["pid"]),
            port=int(data.get("port", C.DSH_DEFAULT_PORT)),
            started_at=float(data.get("started_at", time.time())),
            owner=data.get("owner", "dsh-work"),
        )


def _is_process_alive(pid: int) -> bool:
    """检查进程是否存活（Windows 实现）。"""
    if pid <= 0:
        return False
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # 回退：尝试 os.kill(pid, 0)
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except OSError:
            return False


def _kill_process(pid: int) -> bool:
    """终止进程（Windows 实现）。"""
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_TERMINATE = 0x0001
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False


class PidLock:
    """DSH 进程 PID 文件锁。

    使用 portalocker 实现跨进程文件锁，确保多客户端实例不会争抢同一 DSH。
    """

    def __init__(self, pid_file: Path | None = None):
        self.pid_file = pid_file or get_pid_file_path()
        self._lock_handle = None

    def read(self) -> PidInfo | None:
        """读取 PID 文件内容，不存在返回 None。"""
        if not self.pid_file.exists():
            return None
        try:
            with open(self.pid_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return PidInfo.from_dict(data)
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def is_owner(self, current_pid: int | None = None) -> bool:
        """判断当前客户端是否是 DSH 的启动者（PID 一致）。"""
        info = self.read()
        if info is None:
            return False
        current_pid = current_pid or os.getpid()
        return info.pid == current_pid or info.owner == "dsh-work"

    def is_dsh_alive(self) -> bool:
        """检查 PID 文件指向的 DSH 进程是否存活。"""
        info = self.read()
        if info is None:
            return False
        return _is_process_alive(info.pid)

    def kill_stale(self) -> bool:
        """杀死僵死的 DSH 进程（PID 文件存在但进程已退出时清理）。"""
        info = self.read()
        if info is None:
            return False
        if not _is_process_alive(info.pid):
            log.info("发现僵死 DSH 进程 PID=%d，清理 PID 文件", info.pid)
            self.release()
            return True
        # 进程存活但需要强制终止（如端口冲突）
        if _kill_process(info.pid):
            log.info("已终止 DSH 进程 PID=%d", info.pid)
            self.release()
            return True
        return False

    def acquire(self, pid: int, port: int) -> None:
        """写入 PID 文件，声明 DSH 进程所有权。

        使用 portalocker 加文件锁，防止多客户端同时写入。
        """
        try:
            import portalocker
        except ImportError:
            log.warning("portalocker 未安装，PID 锁退化为普通文件写入")
            portalocker = None

        info = PidInfo(pid=pid, port=port, started_at=time.time(), owner="dsh-work")
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)

        mode = "a+" if portalocker else "w"
        flags = portalocker.LOCK_EX | portalocker.LOCK_NB if portalocker else 0

        if portalocker:
            self._lock_handle = open(self.pid_file, mode, encoding="utf-8")
            try:
                portalocker.lock(self._lock_handle, flags)
            except portalocker.LockException:
                self._lock_handle.close()
                self._lock_handle = None
                raise PidLockError("PID 文件被其他进程锁定")

        with open(self.pid_file, "w", encoding="utf-8") as f:
            if portalocker and self._lock_handle:
                # 已加锁，直接写入
                f.seek(0)
                f.truncate()
            json.dump(info.to_dict(), f, ensure_ascii=False, indent=2)
        log.info("已声明 DSH 进程所有权 PID=%d port=%d", pid, port)

    def release(self) -> None:
        """释放 PID 文件锁（删除文件）。"""
        if self._lock_handle:
            try:
                self._lock_handle.close()
            except Exception:
                pass
            self._lock_handle = None
        try:
            if self.pid_file.exists():
                self.pid_file.unlink()
                log.info("已释放 PID 文件锁")
        except OSError as e:
            log.warning("释放 PID 文件失败: %s", e)

    def terminate_dsh(self) -> bool:
        """终止本客户端启动的 DSH 进程（仅当拥有所有权时）。"""
        if not self.is_owner():
            log.info("无 DSH 进程所有权，不终止（用户手动启动的 DSH 保留运行）")
            return False
        info = self.read()
        if info is None:
            return False
        if _kill_process(info.pid):
            log.info("已终止 DSH 进程 PID=%d", info.pid)
            self.release()
            return True
        return False
