"""工具层：日志、PID 锁、高 DPI 设置。"""

from .high_dpi import setup_high_dpi
from .logger import get_logger
from .pid_lock import PidLock, PidLockError

__all__ = ["get_logger", "PidLock", "PidLockError", "setup_high_dpi"]
