"""文件更改追踪 + 一键还原。

工作流程：
    1. snapshot(paths)  → 对指定文件/目录做基线快照（内容字节 + mtime + hash）
    2. scan_changed()   → 对比基线与磁盘当前状态，返回变更集合
    3. diff_lines(path) → 统一 diff 格式的行级差异
    4. restore_one(path, force=False) / restore_all(force=False)
                          → 冲突保护后精确写回基线内容

冲突保护：还原前比对"当前文件 hash"与"变更扫描时记录的 modified_hash"，
不一致说明用户或其他进程又改过，默认拒绝还原；force=True 时强制覆盖。

持久化：快照同时保存在内存 + 本地 user_data/file_snapshots/<session_id>.json，
崩溃后重启仍可还原。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from ..paths import Paths
from ..utils.logger import get_logger

log = get_logger("core.file_tracker")


# =====================================================================
#  数据结构
# =====================================================================

@dataclass
class FileSnapshot:
    """单文件的基线快照。"""
    path: str                      # 绝对路径
    size: int = 0                  # 基线字节数（deleted 基线时 0）
    mtime: float = 0.0             # 基线修改时间
    sha256: str = ""               # 基线内容 sha256（删除基线时为空）
    content_b64: str = ""          # 基线内容 base64（删除基线时为空）
    existed: bool = True           # 基线时文件是否存在

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> FileSnapshot:
        return cls(**{k: d.get(k, getattr(cls, k, None) if False else "")
                      for k in ["path", "size", "mtime", "sha256", "content_b64", "existed"]})


@dataclass
class ChangedFile:
    """scan_changed() 返回的单条变更记录。"""
    path: str
    status: str                    # "modified" / "added" / "deleted"
    baseline_sha256: str = ""      # 基线 hash（added 为空，deleted 为基线 hash）
    modified_sha256: str = ""      # 当前 hash（deleted 为空）
    modified_size: int = 0         # 当前字节数（deleted 为 0）
    modified_mtime: float = 0.0    # 当前修改时间
    conflict_mark: str = ""        # 还原冲突标记（UI 层用）

    def to_dict(self) -> dict:
        return asdict(self)


class ConflictError(RuntimeError):
    """还原冲突：当前文件与变更扫描时记录的 hash 不一致。"""

    def __init__(self, path: str, current_hash: str, expected_hash: str):
        super().__init__(
            f"文件已被外部修改，拒绝还原：{path}\n"
            f"expected modified hash = {expected_hash[:12]}…\n"
            f"current  hash          = {current_hash[:12]}…\n"
            f"如需强制覆盖请传 force=True。"
        )
        self.path = path
        self.current_hash = current_hash
        self.expected_hash = expected_hash


# =====================================================================
#  核心追踪器
# =====================================================================

class FileChangeTracker:
    """文件变更追踪器（每个 Session 绑定一个独立实例）。"""

    def __init__(self, session_id: str = "global",
                 storage_dir: Path | None = None):
        self.session_id = session_id
        self._storage_dir = Path(storage_dir) if storage_dir else (
            Paths.user_data() / "file_snapshots"
        )
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots: dict[str, FileSnapshot] = {}  # abs_path -> snapshot
        self._last_scan: dict[str, ChangedFile] = {}   # abs_path -> 最近 scan 的变更
        self._lock = threading.RLock()
        # 监听回调（当 scan_changed 检测到变更时触发）
        self._listeners: list[Callable[[list[ChangedFile]], None]] = []

    # ------------------------------------------------------------------
    #  监听
    # ------------------------------------------------------------------

    def add_listener(self, cb: Callable[[list[ChangedFile]], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        try:
            self._listeners.remove(cb)
        except ValueError:
            pass

    def _emit(self, changes: list[ChangedFile]) -> None:
        for cb in list(self._listeners):
            try:
                cb(changes)
            except Exception as e:
                log.warning("file_tracker listener 异常: %s", e)

    # ------------------------------------------------------------------
    #  基础工具
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _iter_files(paths: Iterable[str | Path]) -> Iterable[Path]:
        """展开输入路径（文件直接返回，目录递归展开），去重。

        使用 os.scandir 递归遍历（比 pathlib.rglob 更快）：
         - 直接跳过忽略目录（不再下钻），避免对 node_modules 等大目录做无谓遍历
         - 随即取 stat 校验 is_file，避免 pathlib 对每个路径重复 resolve
        """
        seen: set[Path] = set()
        ignore = {".git", "node_modules", "__pycache__", ".venv", ".DS_Store"}

        for raw in paths:
            p = Path(raw).expanduser().resolve()
            if not p.exists():
                # 不存在：仍把它作为待删除的路径上报（如果后续被创建就能作为 added 识别）
                if p not in seen:
                    seen.add(p)
                    yield p
                continue
            if p.is_file():
                if p not in seen:
                    seen.add(p)
                    yield p
            elif p.is_dir():
                def _walk(dirpath: Path) -> Iterable[Path]:
                    try:
                        with os.scandir(dirpath) as it:
                            for entry in it:
                                # 与原 rglob 行为一致：仅跳过 ignore 集合中的名字（含 .git/node_modules 等）
                                if entry.name in ignore:
                                    continue
                                try:
                                    is_file = entry.is_file()
                                except OSError:
                                    continue
                                if is_file:
                                    fp = Path(entry.path)
                                    if fp not in seen:
                                        seen.add(fp)
                                        yield fp
                                else:
                                    try:
                                        if entry.is_dir():
                                            yield from _walk(Path(entry.path))
                                    except OSError:
                                        continue
                    except OSError as e:
                        log.warning("遍历目录失败 %s: %s", dirpath, e)

                yield from _walk(p)

    def _read_file_safe(self, p: Path) -> bytes | None:
        try:
            return p.read_bytes()
        except OSError as e:
            log.warning("读取文件失败 %s: %s", p, e)
            return None

    # ------------------------------------------------------------------
    #  快照（基线）
    # ------------------------------------------------------------------

    def snapshot(self, paths: str | Path | Iterable[str | Path]) -> int:
        """对一个或多个路径创建基线快照。返回新建立的快照文件数。"""
        if isinstance(paths, (str, Path)):
            paths_iter: Iterable[str | Path] = [paths]
        else:
            paths_iter = paths
        count = 0
        with self._lock:
            for fp in self._iter_files(paths_iter):
                snap = self._build_snapshot(fp)
                self._snapshots[str(fp)] = snap
                count += 1
        log.info("建立文件基线快照 %d 个（session=%s）", count, self.session_id)
        self._save_persistent()
        return count

    def _build_snapshot(self, fp: Path) -> FileSnapshot:
        if fp.exists() and fp.is_file():
            data = self._read_file_safe(fp) or b""
            import base64 as _b64
            return FileSnapshot(
                path=str(fp),
                size=len(data),
                mtime=fp.stat().st_mtime if fp.exists() else 0.0,
                sha256=self._hash_bytes(data),
                content_b64=_b64.b64encode(data).decode("ascii"),
                existed=True,
            )
        # 文件不存在（可能已删除 / 或用户指定了一个预期未来会产生的路径）
        return FileSnapshot(
            path=str(fp),
            existed=False,
        )

    # ------------------------------------------------------------------
    #  变更扫描
    # ------------------------------------------------------------------

    def scan_changed(self) -> list[ChangedFile]:
        """与基线对比，返回变更文件列表（按路径排序）。"""
        results: list[ChangedFile] = []
        with self._lock:
            # 1) 快照中存在的：看现在是否被修改 / 删除
            for path, snap in self._snapshots.items():
                fp = Path(path)
                changed = ChangedFile(path=path, status="modified",
                                      baseline_sha256=snap.sha256)
                if not fp.exists() or not fp.is_file():
                    if snap.existed:
                        changed.status = "deleted"
                        results.append(changed)
                    # 原本就不存在，现在仍不存在 → 无变化
                    continue
                data = self._read_file_safe(fp)
                if data is None:
                    continue  # 读取失败，跳过
                cur_hash = self._hash_bytes(data)
                if cur_hash != snap.sha256:
                    changed.status = "modified"
                    changed.modified_sha256 = cur_hash
                    changed.modified_size = len(data)
                    changed.modified_mtime = fp.stat().st_mtime
                    results.append(changed)

            # 2) 目录基线场景下，扫描磁盘上新增文件：对每个快照的父目录做一次遍历
            extra_parents: set[Path] = set()
            for snap in self._snapshots.values():
                if snap.existed:
                    extra_parents.add(Path(snap.path).parent)
            scanned_keys = set(self._snapshots.keys())
            for parent in extra_parents:
                if not parent.exists() or not parent.is_dir():
                    continue
                try:
                    for child in parent.iterdir():
                        if not child.is_file():
                            continue
                        key = str(child.resolve())
                        if key in scanned_keys:
                            continue
                        data = self._read_file_safe(child)
                        if data is None:
                            continue
                        cf = ChangedFile(
                            path=key,
                            status="added",
                            modified_sha256=self._hash_bytes(data),
                            modified_size=len(data),
                            modified_mtime=child.stat().st_mtime,
                        )
                        results.append(cf)
                        scanned_keys.add(key)
                except OSError as e:
                    log.warning("目录遍历失败 %s: %s", parent, e)

            # 记录本次扫描结果（用于冲突保护）
            self._last_scan = {c.path: c for c in results}

        results.sort(key=lambda c: c.path)
        if results:
            self._emit(results)
        return results

    def get_last_changes(self) -> list[ChangedFile]:
        """返回上次 scan_changed 的结果（UI 刷新用，不重新扫描）。"""
        with self._lock:
            return sorted(self._last_scan.values(), key=lambda c: c.path)

    # ------------------------------------------------------------------
    #  行级 Diff
    # ------------------------------------------------------------------

    def diff_lines(self, path: str) -> str:
        """返回 path 的统一 diff（基线 vs 当前）。无变更或无基线时返回空字符串。"""
        import base64 as _b64
        with self._lock:
            snap = self._snapshots.get(path)
            if snap is None:
                return ""
            if not snap.existed:
                baseline_text = ""
            else:
                try:
                    baseline_text = _b64.b64decode(snap.content_b64).decode(
                        "utf-8", errors="replace"
                    )
                except Exception as e:
                    log.warning("解码基线内容失败 %s: %s", path, e)
                    baseline_text = ""
            fp = Path(path)
            if fp.exists() and fp.is_file():
                raw = self._read_file_safe(fp)
                if raw is None:
                    current_text = ""
                else:
                    current_text = raw.decode("utf-8", errors="replace")
            else:
                current_text = ""
        baseline_lines = baseline_text.splitlines(keepends=True)
        current_lines = current_text.splitlines(keepends=True)
        if not baseline_lines and not current_lines:
            return ""
        diff = difflib.unified_diff(
            baseline_lines, current_lines,
            fromfile=f"a/{Path(path).name} (基线)",
            tofile=f"b/{Path(path).name} (当前)",
            n=3,
        )
        return "".join(diff)

    # ------------------------------------------------------------------
    #  还原
    # ------------------------------------------------------------------

    def restore_one(self, path: str, force: bool = False) -> None:
        """还原单文件到基线状态。

        Raises:
            FileNotFoundError: 无此路径的基线
            ConflictError: 当前内容 hash != 变更扫描 hash 且 force=False
        """
        import base64 as _b64
        with self._lock:
            snap = self._snapshots.get(path)
            if snap is None:
                raise FileNotFoundError(f"无基线快照，无法还原: {path}")

            fp = Path(path)

            # 冲突保护：若有上次扫描的 modified 结果，比对 hash
            if not force and path in self._last_scan:
                scan_rec = self._last_scan[path]
                expected = scan_rec.modified_sha256
                if expected:  # added/deleted 可能无 expected，此时跳过冲突检查
                    if fp.exists() and fp.is_file():
                        cur_data = self._read_file_safe(fp) or b""
                        current_hash = self._hash_bytes(cur_data)
                        if current_hash != expected:
                            raise ConflictError(path, current_hash, expected)
                    # deleted：期望不存在，若此时文件已不存在，无冲突

            # 还原：基线存在 → 写回 bytes；基线不存在 → 删除
            if not snap.existed:
                if fp.exists():
                    try:
                        fp.unlink()
                    except OSError as e:
                        raise RuntimeError(f"删除文件失败 {path}: {e}") from e
                return

            data = _b64.b64decode(snap.content_b64)
            fp.parent.mkdir(parents=True, exist_ok=True)
            try:
                # 原子写：先写临时文件再 rename，避免半写损坏
                tmp = fp.with_suffix(fp.suffix + f".tmp_{int(time.time()*1000)}")
                tmp.write_bytes(data)
                tmp.replace(fp)
            except OSError as e:
                raise RuntimeError(f"写回基线文件失败 {path}: {e}") from e

            # 更新 scan 状态，使后续 UI 能显示已还原
            if path in self._last_scan:
                del self._last_scan[path]

    def restore_all(self, force: bool = False) -> tuple[int, list[tuple[str, str]]]:
        """一键还原所有已扫描变更。返回 (成功数, [(path, error_str), ...])。"""
        errors: list[tuple[str, str]] = []
        ok = 0
        # 先锁定路径集合，避免中途变化
        with self._lock:
            paths = list(self._last_scan.keys())
        for p in paths:
            try:
                self.restore_one(p, force=force)
                ok += 1
            except ConflictError as e:
                errors.append((p, f"冲突: {e}"))
            except Exception as e:
                errors.append((p, str(e)))
        return ok, errors

    # ------------------------------------------------------------------
    #  持久化
    # ------------------------------------------------------------------

    def _storage_path(self) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_"
                       for c in self.session_id) or "global"
        return self._storage_dir / f"{safe}.json"

    def _save_persistent(self) -> None:
        try:
            data = {
                "session_id": self.session_id,
                "saved_at": time.time(),
                "snapshots": [s.to_dict() for s in self._snapshots.values()],
            }
            self._storage_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            log.warning("持久化快照失败: %s", e)

    def load_persistent(self) -> int:
        """从磁盘加载上次保存的快照。返回加载数量。"""
        p = self._storage_path()
        if not p.exists():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            snaps = data.get("snapshots") or []
            loaded = 0
            with self._lock:
                for d in snaps:
                    try:
                        s = FileSnapshot.from_dict(d)
                        self._snapshots[s.path] = s
                        loaded += 1
                    except Exception:
                        continue
            log.info("从磁盘加载文件快照 %d 个（session=%s）", loaded, self.session_id)
            return loaded
        except (OSError, ValueError) as e:
            log.warning("加载快照失败 %s: %s", p, e)
            return 0

    def clear(self) -> None:
        """清空所有快照。"""
        with self._lock:
            self._snapshots.clear()
            self._last_scan.clear()
        try:
            p = self._storage_path()
            if p.exists():
                p.unlink()
        except OSError:
            pass
