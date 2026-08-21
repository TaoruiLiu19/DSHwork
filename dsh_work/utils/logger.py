"""日志系统。

按天轮转存储于 ~/.dsh-work/logs/，记录：
- HTTP RPC 请求/响应（脱敏 Authorization 头）
- WS 连接/断线时间戳
- 技能加载失败堆栈
- 崩溃回溯

帮助菜单"导出诊断压缩包"可一键打包日志 + 基础配置（不含 Key）。
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .. import constants as C
from ..config import get_logs_dir

_LOGGER_NAME = "dsh_work"
_initialized = False

# 脱敏正则：匹配 Authorization 头与常见 Key 模式
_SENSITIVE_PATTERNS = [
    # Authorization: Bearer xxx
    re.compile(r"(Authorization\s*[:=]\s*Bearer\s+)([^\s,]+)", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[:=]\s*)([^\s,]+)", re.IGNORECASE),
    re.compile(r"(sk-[a-zA-Z0-9]{8})[a-zA-Z0-9]+"),
]


class SensitiveFilter(logging.Filter):
    """脱敏过滤器：将日志中的 API Key / Authorization 头替换为掩码。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = record.msg
            for pat in _SENSITIVE_PATTERNS:
                if pat.groups == 2:
                    msg = pat.sub(r"\1***MASKED***", msg)
                else:
                    msg = pat.sub(r"\1***", msg)
            record.msg = msg
        return True


def _init_logger() -> logging.Logger:
    global _initialized
    logger = logging.getLogger(_LOGGER_NAME)
    if _initialized:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    # 避免 handler 异常（如 GUI 进程 stderr 无效 Bad file descriptor）打印 Traceback 干扰用户
    logging.raiseExceptions = False

    log_dir = get_logs_dir()
    log_file = log_dir / "dsh_work.log"

    # 文件 handler：轮转
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=C.LOG_MAX_BYTES,
        backupCount=C.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    file_handler.addFilter(SensitiveFilter())
    logger.addHandler(file_handler)

    # 控制台 handler：仅当有有效 stderr/stdout 时才添加（如终端下运行）
    import sys as _sys
    _stderr_ok = (
        getattr(_sys, "stderr", None) is not None
        and hasattr(_sys.stderr, "fileno")
        and getattr(_sys.stderr, "isatty", lambda: False)()
    )
    # 非 GUI 环境：即使没 tty，只要 fileno 不抛异常也加上（CI / 重定向场景）
    if not _stderr_ok:
        try:
            _sys.stderr.fileno()
            _stderr_ok = True
        except (OSError, AttributeError):
            _stderr_ok = False

    if _stderr_ok:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(
            logging.Formatter("[%(levelname)s] %(message)s")
        )
        console_handler.addFilter(SensitiveFilter())
        logger.addHandler(console_handler)

    _initialized = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """获取 logger 实例。

    Args:
        name: 子模块名（如 'api.http_client'），自动加上 dsh_work 前缀。
    """
    _init_logger()
    if name:
        return logging.getLogger(f"{_LOGGER_NAME}.{name}")
    return logging.getLogger(_LOGGER_NAME)


def export_diagnostics_bundle(target_path: Path) -> Path:
    """导出诊断压缩包：日志 + 基础配置（不含 Key）。

    供帮助菜单"一键导出诊断日志"使用。
    """
    import json
    import zipfile

    from ..config import UserConfig

    bundle = target_path / f"dsh-work-diagnostics-{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        log_dir = get_logs_dir()
        for log_file in log_dir.glob("*.log*"):
            zf.write(log_file, f"logs/{log_file.name}")
        # 配置快照（脱敏）
        cfg = UserConfig.load()
        snapshot = cfg.to_dict()
        snapshot.pop("custom_dsh_endpoint", None)  # 不导出端点
        zf.writestr("config_snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=2))
    return bundle
