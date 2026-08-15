"""会话日志监控 + 任务完成系统通知（移植自 dsh_desktop session-watcher.js）。

核心设计（与对方 Electron 版完全对齐）：
  1. 扫描 DSH sessions 目录下所有 **/session.jsonl.zstd 日志文件
  2. 结构扫描 zstd 帧（只找帧边界，不全量解压）→ 增量只解压新增完整帧
  3. 首次见到会话 → 只建立基线（baseline），不触发历史通知（避免启动时一堆历史 toast）
  4. 增量解码 JSONL → expand 存储行（text-chunks/tool-call-chunks）→ 统计 turn/end 计数
  5. 有 turn 事件按 turn/end 计数，否则按 assistant/message 兜底计数
  6. 过滤 subagent（delegationDepth > 0）不弹通知（子 Agent 噪声）
  7. 通知通过回调 on_turn_end 发出（通常调用 SystemTray.notify()）

性能优化（完全照搬对方 JS 版）：
  - 目录枚举缓存 5 秒（避免每 3 秒递归整个 sessions 目录造成桌面卡顿）
  - 首扫分批限流（先让窗口绘制，分批解码，避免启动时主进程被大量会话日志卡死）
  - 增量读取文件尾部（readTail from offset），不每次全量读盘
  - 首次基线只解析首帧头部（获取 session id/title/cwd），不解码历史所有帧

注：pyzstd 是轻量级 CFFI 实现，性能与 node:zlib 相当；
若将来打包 PyInstaller 报错，把 pyzstd 加到 hiddenimports 即可。
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:
    import pyzstd  # type: ignore
    _HAS_PYZSTD = True
except ImportError:  # pragma: no cover - 降级保护：如果 pyzstd 不可用则全程不工作，不崩溃
    _HAS_PYZSTD = False

from ..utils.logger import get_logger

log = get_logger("core.session_watcher")

# ============================================================================
# zstd 帧结构扫描（与 scanZstdFrames JS 版字节级对齐）
# ============================================================================
ZSTD_MAGIC = 0xFD2FB528  # = 4247762216  little-endian: 28 B5 2F FD


@dataclass
class FrameRange:
    start: int  # 帧起点（相对传入 buffer 的偏移）
    end: int    # 帧终点（下一字节起始）


@dataclass
class ScanResult:
    frames: list[FrameRange] = field(default_factory=list)
    torn_start: int = -1  # 若尾部存在撕裂帧（未写满），记录其起始偏移


def scan_zstd_frames(buffer: bytes) -> ScanResult:
    """结构扫描 zstd 帧（纯字节运算，不解压）。

    算法与 dsh session-persistence-jsonl backend 的 scanZstdFrames 完全一致。
    Returns: { frames: [FrameRange(start,end), ...], tornStart }
    """
    frames: list[FrameRange] = []
    offset = 0
    n = len(buffer)
    while offset < n:
        start = offset
        if n - offset < 4:
            return ScanResult(frames=frames, torn_start=start)
        # magic check (4 bytes LE)
        if struct.unpack_from("<I", buffer, offset)[0] != ZSTD_MAGIC:
            return ScanResult(frames=frames, torn_start=start)
        offset += 4
        if offset == n:
            return ScanResult(frames=frames, torn_start=start)
        descriptor = buffer[offset]
        offset += 1
        if (descriptor & 24) != 0:  # reserved bits must be 0
            return ScanResult(frames=frames, torn_start=start)
        content_size_flag = descriptor >> 6
        single_segment = (descriptor & 32) != 0
        checksum = (descriptor & 4) != 0
        dictionary_flag = descriptor & 3
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (
            (1 if single_segment else 0)
            if content_size_flag == 0
            else (1 << content_size_flag)
        )
        remaining_header_bytes = (
            (0 if single_segment else 1) + dictionary_bytes + content_size_bytes
        )
        if n - offset < remaining_header_bytes:
            return ScanResult(frames=frames, torn_start=start)
        offset += remaining_header_bytes
        # scan blocks until last_block
        while True:
            if n - offset < 3:
                return ScanResult(frames=frames, torn_start=start)
            block_header = buffer[offset] | (buffer[offset + 1] << 8) | (buffer[offset + 2] << 16)
            offset += 3
            last_block = (block_header & 1) != 0
            block_type = (block_header >> 1) & 3
            block_size = block_header >> 3
            if block_type == 3:  # reserved block type -> corrupt
                return ScanResult(frames=frames, torn_start=start)
            payload_bytes = 1 if block_type == 1 else block_size
            if n - offset < payload_bytes:
                return ScanResult(frames=frames, torn_start=start)
            offset += payload_bytes
            if last_block:
                break
        if checksum:
            if n - offset < 4:
                return ScanResult(frames=frames, torn_start=start)
            offset += 4
        frames.append(FrameRange(start=start, end=offset))
    return ScanResult(frames=frames)


def _decode_frame(frame_bytes: bytes) -> str:
    """解压一帧 zstd → UTF-8 文本。失败返回空串。"""
    if not _HAS_PYZSTD:
        return ""
    try:
        return pyzstd.decompress(frame_bytes).decode("utf-8", errors="replace")
    except Exception as e:  # 损坏帧 → 跳过，下次重试
        log.debug("zstd 帧解压失败: %s", e)
        return ""


# ============================================================================
# JSONL 存储行展开（storage rows pack many chunk events）
# ============================================================================
def expand_row(raw_line: str) -> list[dict]:
    """展开一行 JSONL。text-chunks/reasoning-chunks/tool-call-chunks 会拆成多条事件。"""
    if not raw_line:
        return []
    try:
        row = json.loads(raw_line)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(row, dict):
        return []
    rtype = row.get("type")
    if rtype in ("text-chunks", "reasoning-chunks"):
        data = row.get("data") or {}
        texts = data.get("texts") if isinstance(data, dict) else None
        return list(texts) if isinstance(texts, list) else []
    if rtype == "tool-call-chunks":
        data = row.get("data") or {}
        args = data.get("args") if isinstance(data, dict) else None
        return list(args) if isinstance(args, list) else []
    return [row]


# ============================================================================
# 文件级追踪记录
# ============================================================================
@dataclass
class FileRecord:
    size: int = 0               # 文件字节数
    consumed: int = 0           # 已消费到的字节偏移
    header: Optional[dict] = None   # session 头部（type='session'）
    title: str = ""             # 会话标题（session/title 事件）
    baseline: bool = False      # 是否已建立基线（首次见过）
    has_turn_events: bool = False  # 会话中是否出现过 turn 事件（决定计数语义）


@dataclass
class TurnEndEvent:
    """任务完成事件，传给 on_turn_end 回调。"""
    title: str          # 通知标题（会话标题或默认文案）
    body: str           # 通知正文（工作目录 + 短会话 ID）
    session_id: str     # 完整会话 ID（可用于点击通知定位）
    cwd: str            # 工作区目录
    count: int          # 本轮新增完成轮数（>1 表示多轮批量完成）


# ============================================================================
# SessionWatcher 主类
# ============================================================================
class SessionWatcher:
    """DSH 会话日志监控器。

    用法：
        watcher = SessionWatcher(
            sessions_dir=get_dsh_sessions_dir(),
            on_turn_end=lambda ev: tray.notify(ev.title, ev.body),
        )
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(
        self,
        sessions_dir: str | os.PathLike,
        on_turn_end: Optional[Callable[[TurnEndEvent], None]] = None,
    ) -> None:
        self._sessions_dir = Path(sessions_dir)
        self._on_turn_end = on_turn_end or (lambda ev: None)
        # absPath -> FileRecord
        self._files: dict[str, FileRecord] = {}
        # 目录枚举缓存 5s（与 JS 版一致）
        self._dir_cache_at: float = 0.0
        self._dir_cache_files: list[str] = []

        self._timer: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    # 目录枚举 + 缓存
    # ------------------------------------------------------------------
    def _list_logs(self) -> list[str]:
        now = time.time()
        if now - self._dir_cache_at < 5.0:
            return self._dir_cache_files
        out: list[str] = []
        if self._sessions_dir.is_dir():
            try:
                for root, _dirs, files in os.walk(self._sessions_dir):
                    for f in files:
                        if f == "session.jsonl.zstd":
                            out.append(os.path.join(root, f))
            except OSError as e:
                log.warning("sessions 目录遍历失败: %s", e)
        self._dir_cache_at = now
        self._dir_cache_files = out
        return out

    # ------------------------------------------------------------------
    # 增量读取文件尾部（不读整个文件）
    # ------------------------------------------------------------------
    @staticmethod
    def _read_tail(file_path: str, offset: int, file_size: int) -> bytes:
        length = file_size - offset
        if length <= 0:
            return b""
        tail = bytearray(length)
        try:
            with open(file_path, "rb") as fd:
                fd.seek(offset)
                pos = 0
                while pos < length:
                    n = fd.readinto(memoryview(tail)[pos:])
                    if n <= 0:
                        break
                    pos += n
            return bytes(tail[:pos])
        except OSError:
            return b""

    # ------------------------------------------------------------------
    # 单文件处理（核心）
    # ------------------------------------------------------------------
    def _process(self, file_path: str) -> bool:
        """处理单个日志文件。返回 True 表示该文件有新内容被消费。"""
        # 1. 取得文件 size
        try:
            st = os.stat(file_path)
        except OSError:
            self._files.pop(file_path, None)
            return False

        rec = self._files.get(file_path)
        if rec is None:
            rec = FileRecord()
            self._files[file_path] = rec

        # 文件被截断/重写（如 repair 脚本）→ 重置并重新基线
        if st.st_size < rec.consumed:
            rec.size = 0
            rec.consumed = 0
            rec.header = None
            rec.title = ""
            rec.baseline = False
            rec.has_turn_events = False

        first = not rec.baseline
        read_from = rec.consumed
        tail = self._read_tail(file_path, read_from, st.st_size)

        # 增量模式下，尾部不是帧起点 → 归零重新基线（文件被拼接异常/重写）
        if not first and len(tail) >= 4:
            if struct.unpack_from("<I", tail, 0)[0] != ZSTD_MAGIC:
                rec.consumed = 0
                rec.header = None
                rec.title = ""
                rec.baseline = False
                rec.has_turn_events = False
                return self._process(file_path)

        scan = scan_zstd_frames(tail)

        # ----------------------------
        # 首次：建立基线（只解析首帧头，不解码历史，避免启动卡顿）
        # ----------------------------
        if first:
            if scan.frames:
                f0 = scan.frames[0]
                text = _decode_frame(tail[f0.start:f0.end])
                if text:
                    first_line = text.split("\n", 1)[0]
                    try:
                        h = json.loads(first_line)
                        if isinstance(h, dict) and h.get("type") == "session":
                            rec.header = h
                    except (json.JSONDecodeError, ValueError):
                        pass  # 下次重试
                rec.consumed = read_from + scan.frames[-1].end
            rec.baseline = True
            rec.size = st.st_size
            return True  # 计为"做了重活"（供分批限流）

        # ----------------------------
        # 增量：解码 consumed 之后的所有新完整帧
        # ----------------------------
        turn_ends = 0
        assistant_messages = 0
        consumed = read_from
        for fr in scan.frames:
            text = _decode_frame(tail[fr.start:fr.end])
            if not text:
                break
            for line in text.split("\n"):
                if not line:
                    continue
                for ev in expand_row(line):
                    if not isinstance(ev, dict):
                        continue
                    etype = ev.get("type")
                    if etype == "session/title":
                        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
                        t = data.get("title")
                        if isinstance(t, str):
                            rec.title = t
                    if etype in ("turn/start", "turn/end"):
                        rec.has_turn_events = True
                    if etype == "turn/end":
                        turn_ends += 1
                    if etype == "assistant/message":
                        assistant_messages += 1
            consumed = read_from + fr.end

        rec.consumed = consumed
        rec.size = st.st_size

        count = turn_ends if rec.has_turn_events else assistant_messages
        if count > 0:
            self._emit(rec, count)
        return count > 0 or consumed > read_from

    # ------------------------------------------------------------------
    # 发出通知
    # ------------------------------------------------------------------
    def _emit(self, rec: FileRecord, count: int) -> None:
        header = rec.header or {}
        # subagent 日志是噪声 → 过滤（与 JS 版 delegationDepth 判断一致）
        if isinstance(header.get("delegationDepth"), int) and header["delegationDepth"] > 0:
            return

        title = rec.title if rec.title else "DSH 任务完成"
        parts: list[str] = []
        cwd = header.get("cwd") if isinstance(header.get("cwd"), str) else ""
        sid = header.get("id") if isinstance(header.get("id"), str) else ""
        if cwd:
            parts.append(os.path.basename(os.path.normpath(cwd)))
        if sid:
            parts.append("会话 " + sid[-8:])
        body = " · ".join(parts)
        if count > 1:
            body += f"（{count} 轮任务完成）"
        try:
            self._on_turn_end(TurnEndEvent(
                title=title,
                body=body,
                session_id=sid,
                cwd=cwd,
                count=count,
            ))
        except Exception as e:
            log.warning("on_turn_end 回调异常: %s", e)

    # ------------------------------------------------------------------
    # 一次扫描（可供 start() 定时调用或外部手动调用）
    # ------------------------------------------------------------------
    def scan(self, max_changed: int = float("inf")) -> bool:  # type: ignore[assignment]
        any_changed = False
        changed = 0
        for fp in self._list_logs():
            try:
                grew = self._process(fp)
                if grew:
                    any_changed = True
                    changed += 1
                    if changed >= max_changed:
                        break
            except Exception as e:
                log.warning("处理会话日志失败 %s: %s", fp, e)
        return any_changed

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    def start(self, interval_sec: float = 3.0) -> None:
        """启动后台监控线程（默认每 3 秒扫一次，与 JS 版一致）。

        首扫延后一拍（先让窗口绘制），且分批处理，避免启动时被大量历史会话日志卡死。
        """
        if self._timer is not None:
            return
        self._stop.clear()

        def _run():
            # 首拍：先处理最多 4 个会话（分批限流），让 UI 先画出来
            time.sleep(0.2)
            try:
                self.scan(max_changed=4)
            except Exception as e:
                log.warning("首扫异常: %s", e)
            # 定时循环
            while not self._stop.is_set():
                t0 = time.time()
                try:
                    self.scan()
                except Exception as e:
                    log.warning("定期扫描异常: %s", e)
                elapsed = time.time() - t0
                sleep_for = max(0.05, interval_sec - elapsed)
                self._stop.wait(sleep_for)

        self._timer = threading.Thread(target=_run, name="session-watcher", daemon=True)
        self._timer.start()
        log.info(
            "SessionWatcher 已启动: sessions_dir=%s, interval=%.1fs, pyzstd=%s",
            self._sessions_dir, interval_sec, _HAS_PYZSTD,
        )

    def stop(self) -> None:
        if self._timer is None:
            return
        self._stop.set()
        t = self._timer
        self._timer = None
        t.join(timeout=2.0)
        log.info("SessionWatcher 已停止")
        self._files.clear()
