"""VSCode 风格文件树 + HTML/端口预览的核心逻辑层。

UI 层（PySide/PyQt）可以直接继承这些类，或者把它们作为数据模型 + 业务 API 层。

本文件包含：
  FileTreeNode        — 文件/目录树节点（懒加载子节点、扩展名图标键、大小/修改时间）
  FileTreeModel       — 根目录管理、展开/折叠、过滤（name/扩展名）、打开/删除/重命名 API
  PortManager         — 枚举本机监听 TCP 端口、记录"被预览端口→预览键"映射、判断端口可达
  HtmlPreviewServer   — 本地 HTTP 静态服务（threading+http.server），按 token 托管 HTML/目录，
                        生成预览 URL；可托管 .html 单文件或任意静态资源目录
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable
from typing import Any

from ..utils.logger import get_logger

log = get_logger("core.file_tree")


# =====================================================================
#  文件树数据结构
# =====================================================================

@dataclass
class FileTreeNode:
    """文件树节点（文件或目录）。"""

    path: Path
    parent: "FileTreeNode | None" = None
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0
    children_loaded: bool = False
    expanded: bool = False
    # 子节点（加载后填充，按 目录优先 → 字母序 排序）
    children: list["FileTreeNode"] = field(default_factory=list)
    # 扩展名/图标键缓存（path 创建后不可变，缓存安全；避免渲染时光标/扩展名反复计算）
    _ext_cache: str = field(default="", init=False, repr=False)
    _icon_cache: str = field(default="", init=False, repr=False)

    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.path.name or str(self.path)

    @property
    def extension(self) -> str:
        if not self._ext_cache:
            self._ext_cache = self.path.suffix.lower()
        return self._ext_cache

    @property
    def icon_key(self) -> str:
        """给 UI 层用的图标分类键（folder / file-html / file-py / file-image / file-default）。"""
        if self.is_dir:
            return "folder"
        if self._icon_cache:
            return self._icon_cache
        ext = self.extension
        if ext in {".html", ".htm"}:
            key = "file-html"
        elif ext in {".py", ".pyw"}:
            key = "file-py"
        elif ext in {".js", ".ts", ".jsx", ".tsx"}:
            key = "file-js"
        elif ext in {".css", ".scss", ".less"}:
            key = "file-css"
        elif ext in {".json", ".yaml", ".yml", ".toml"}:
            key = "file-config"
        elif ext in {".md", ".txt", ".rst"}:
            key = "file-doc"
        elif ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
            key = "file-image"
        elif ext in {".zip", ".tar", ".gz", ".7z", ".rar"}:
            key = "file-archive"
        else:
            key = "file-default"
        self._icon_cache = key
        return key

    def load_children(self, show_hidden: bool = False) -> None:
        """懒加载子节点（已加载时直接返回）。"""
        if self.children_loaded:
            return
        if not self.is_dir:
            self.children_loaded = True
            return
        results: list[FileTreeNode] = []
        try:
            with os.scandir(self.path) as it:
                for entry in it:
                    try:
                        if not show_hidden and entry.name.startswith("."):
                            continue
                        p = Path(entry.path)
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        node = FileTreeNode(
                            path=p,
                            parent=self,
                            is_dir=entry.is_dir(follow_symlinks=False),
                            size=st.st_size,
                            mtime=st.st_mtime,
                        )
                        results.append(node)
                    except OSError:
                        continue
        except OSError as e:
            log.warning("扫描目录失败 %s: %s", self.path, e)
        # 排序：目录优先 → 名称字母序（不区分大小写）
        results.sort(key=lambda n: (0 if n.is_dir else 1, n.name.lower()))
        self.children = results
        self.children_loaded = True

    def unload_children(self) -> None:
        """释放子节点（UI 折叠后调用以省内存）。"""
        for c in self.children:
            c.parent = None
        self.children.clear()
        self.children_loaded = False
        self.expanded = False

    def refresh(self) -> None:
        """强制重新加载。"""
        self.unload_children()
        self.load_children()


class FileTreeModel:
    """文件树模型（作为 UI QAbstractItemModel 的后端数据源）。"""

    # 忽略目录名（与 VSCode 默认一致的子集）
    DEFAULT_IGNORE_NAMES = {
        ".git", "node_modules", "__pycache__", ".venv", "venv",
        ".DS_Store", ".idea", ".vscode", "dist", "build", ".next",
    }

    def __init__(self, root_paths: Iterable[str | Path] | None = None):
        self._roots: list[FileTreeNode] = []
        self._show_hidden = False
        self._ignore_names: set[str] = set(self.DEFAULT_IGNORE_NAMES)
        if root_paths is not None:
            for rp in root_paths:
                self.add_root(rp)

    # ---------------- 根目录管理 ----------------

    def add_root(self, path: str | Path) -> FileTreeNode | None:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            log.warning("添加的根目录不存在: %s", p)
            return None
        # 去重：同一个绝对路径不重复加
        for r in self._roots:
            if r.path == p:
                return r
        try:
            st = p.stat()
        except OSError as e:
            log.warning("无法读取根目录 %s: %s", p, e)
            return None
        node = FileTreeNode(path=p, parent=None, is_dir=p.is_dir(),
                            size=st.st_size, mtime=st.st_mtime)
        self._roots.append(node)
        return node

    def remove_root(self, path: str | Path) -> bool:
        p = Path(path).expanduser().resolve()
        for i, r in enumerate(self._roots):
            if r.path == p:
                del self._roots[i]
                return True
        return False

    @property
    def roots(self) -> list[FileTreeNode]:
        return list(self._roots)

    # ---------------- 配置 ----------------

    @property
    def show_hidden(self) -> bool:
        return self._show_hidden

    @show_hidden.setter
    def show_hidden(self, v: bool) -> None:
        if v == self._show_hidden:
            return
        self._show_hidden = v
        # 已加载的子节点要刷新
        def _visit(n: FileTreeNode) -> None:
            if n.children_loaded:
                n.unload_children()
                n.load_children(show_hidden=self._show_hidden)
            for c in list(n.children):
                _visit(c)
        for r in self._roots:
            _visit(r)

    # ---------------- 工具方法：直接给 UI 菜单项调用 ----------------

    def open_in_system(self, node: FileTreeNode) -> bool:
        """用系统默认程序打开文件（或 reveal in folder）。"""
        try:
            if platform.system() == "Windows":
                os.startfile(str(node.path))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(node.path)])
            else:
                subprocess.Popen(["xdg-open", str(node.path)])
            return True
        except OSError as e:
            log.warning("系统打开失败 %s: %s", node.path, e)
            return False

    def reveal_in_folder(self, node: FileTreeNode) -> bool:
        try:
            sysname = platform.system()
            if sysname == "Windows":
                args = ["explorer", "/select,", str(node.path)]
                subprocess.Popen(args)
            elif sysname == "Darwin":
                subprocess.Popen(["open", "-R", str(node.path)])
            else:
                folder = node.path if node.is_dir else node.path.parent
                subprocess.Popen(["xdg-open", str(folder)])
            return True
        except OSError as e:
            log.warning("定位文件夹失败 %s: %s", node.path, e)
            return False

    def search(self, keyword: str, max_results: int = 200) -> list[FileTreeNode]:
        """按文件名简单搜索（不区分大小写，子串匹配）。"""
        keyword = (keyword or "").strip().lower()
        results: list[FileTreeNode] = []
        if not keyword:
            return results

        def _visit(n: FileTreeNode) -> bool:
            if len(results) >= max_results:
                return True
            if keyword in n.name.lower():
                results.append(n)
            if n.is_dir:
                if not n.children_loaded:
                    n.load_children(show_hidden=self._show_hidden)
                for c in list(n.children):
                    if _visit(c):
                        return True
            return False

        for r in self._roots:
            if _visit(r):
                break
        return results


# =====================================================================
#  端口管理（枚举、健康探测、预览键注册）
# =====================================================================

@dataclass
class ListeningPort:
    port: int
    pid: int = 0
    process_name: str = ""
    address: str = "127.0.0.1"
    proto: str = "tcp"

    @property
    def url(self) -> str:
        return f"http://{self._host_for_url()}:{self.port}"

    def _host_for_url(self) -> str:
        if self.address in {"0.0.0.0", "::", "*", ""}:
            return "127.0.0.1"
        try:
            ip = ipaddress.ip_address(self.address)
            if isinstance(ip, ipaddress.IPv6Address):
                return f"[{ip}]"
            if ip.is_unspecified:
                return "127.0.0.1"
            return str(ip)
        except ValueError:
            return self.address or "127.0.0.1"

    def is_reachable(self, timeout_ms: int = 500) -> bool:
        """快速 TCP 探测，判断端口是否可连接（服务健康检查用）。"""
        try:
            host = "127.0.0.1" if self.address in {"0.0.0.0", "::", "*", ""} else self.address
            if host == "::":
                host = "::1"
            af = socket.AF_INET6 if ":" in host else socket.AF_INET
            with socket.socket(af, socket.SOCK_STREAM) as s:
                s.settimeout(max(timeout_ms, 1) / 1000.0)
                s.connect((host, self.port))
            return True
        except OSError:
            return False


class PortManager:
    """监听端口枚举与健康检查。"""

    def list_tcp(self) -> list[ListeningPort]:
        """跨平台枚举本机 TCP 监听端口。优先 psutil，否则走 netstat / ss / Get-NetTCPConnection。"""
        ports: list[ListeningPort] = []
        # 优先 psutil
        try:
            import psutil  # 懒加载
            for conn in psutil.net_connections(kind="tcp"):
                if conn.status != "LISTEN" or not conn.laddr:
                    continue
                host, port = conn.laddr
                lp = ListeningPort(
                    port=int(port),
                    pid=conn.pid or 0,
                    address=str(host),
                    proto="tcp",
                )
                if lp.pid:
                    try:
                        lp.process_name = psutil.Process(lp.pid).name()
                    except Exception:
                        pass
                ports.append(lp)
            return self._dedupe_and_sort(ports)
        except ImportError:
            pass
        except Exception as e:
            log.warning("psutil 枚举端口失败: %s", e)

        # 回退：系统命令
        try:
            if platform.system() == "Windows":
                ports = self._list_tcp_windows()
            elif platform.system() == "Darwin":
                ports = self._list_tcp_lsof()
            else:
                ports = self._list_tcp_ss()
        except Exception as e:
            log.warning("系统命令枚举端口失败: %s", e)
            ports = []
        return self._dedupe_and_sort(ports)

    def _dedupe_and_sort(self, ports: list[ListeningPort]) -> list[ListeningPort]:
        seen: set[tuple[str, int]] = set()
        out: list[ListeningPort] = []
        for p in ports:
            key = (p.address, p.port)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        out.sort(key=lambda x: x.port)
        return out

    # ---- Windows ----
    def _list_tcp_windows(self) -> list[ListeningPort]:
        result: list[ListeningPort] = []
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetTCPConnection -State Listen | "
             "Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            return result
        try:
            data = json.loads(cp.stdout)
        except ValueError:
            return result
        items = data if isinstance(data, list) else [data]
        for it in items:
            try:
                port = int(it.get("LocalPort", 0))
                if port <= 0:
                    continue
                lp = ListeningPort(
                    port=port,
                    pid=int(it.get("OwningProcess", 0) or 0),
                    address=str(it.get("LocalAddress", "127.0.0.1")),
                    proto="tcp",
                )
                if lp.pid:
                    # 尝试拿进程名
                    try:
                        wmic = subprocess.run(
                            ["powershell", "-NoProfile", "-Command",
                             f"(Get-Process -Id {lp.pid} -ErrorAction SilentlyContinue).Name"],
                            capture_output=True, text=True, timeout=3,
                        )
                        if wmic.returncode == 0 and wmic.stdout.strip():
                            lp.process_name = wmic.stdout.strip().splitlines()[0]
                    except Exception:
                        pass
                result.append(lp)
            except Exception:
                continue
        return result

    # ---- Linux ss ----
    def _list_tcp_ss(self) -> list[ListeningPort]:
        cp = subprocess.run(
            ["ss", "-ltnHp"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return self._list_tcp_netstat_generic()
        out: list[ListeningPort] = []
        for line in cp.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            local = parts[3]
            host, port = local.rsplit(":", 1) if ":" in local else ("", local)
            host = host.strip("[]")
            try:
                p = int(port)
            except ValueError:
                continue
            pid, name = 0, ""
            # last field like users:((("name",pid=...,fd=...)))
            for seg in parts[4:]:
                if "pid=" in seg and "name=" in seg:
                    try:
                        name = seg.split('"')[1] if '"' in seg else ""
                        pid_s = seg.split("pid=")[1].split(",", 1)[0]
                        pid = int(pid_s)
                    except Exception:
                        pass
            out.append(ListeningPort(port=p, pid=pid, process_name=name,
                                     address=host, proto="tcp"))
        return out

    # ---- macOS lsof ----
    def _list_tcp_lsof(self) -> list[ListeningPort]:
        cp = subprocess.run(
            ["lsof", "-iTCP", "-sTCP:LISTEN", "-Pn", "-F", "pnPc"],
            capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return self._list_tcp_netstat_generic()
        out: list[ListeningPort] = []
        cur: dict[str, Any] = {}
        for line in cp.stdout.splitlines():
            if not line:
                continue
            tag, rest = line[0], line[1:]
            if tag == "p":
                if cur:
                    self._push_from_lsof(cur, out)
                cur = {"pid": int(rest) if rest.isdigit() else 0, "name": ""}
            elif tag == "P":
                pass  # protocol
            elif tag == "n":
                # localhost:8080 or *:8080
                if ":" in rest:
                    host, port_s = rest.rsplit(":", 1)
                    if host == "*":
                        host = "0.0.0.0"
                    if port_s.isdigit():
                        cur["address"] = host
                        cur["port"] = int(port_s)
            elif tag == "c":
                cur["name"] = rest.strip('"')
        if cur:
            self._push_from_lsof(cur, out)
        return out

    def _push_from_lsof(self, cur: dict, out: list[ListeningPort]) -> None:
        p = cur.get("port") or 0
        if p:
            out.append(ListeningPort(
                port=int(p),
                pid=int(cur.get("pid", 0) or 0),
                process_name=str(cur.get("name", "") or ""),
                address=str(cur.get("address", "127.0.0.1")),
                proto="tcp",
            ))

    def _list_tcp_netstat_generic(self) -> list[ListeningPort]:
        cp = subprocess.run(
            ["netstat", "-an"], capture_output=True, text=True, timeout=10,
        )
        if cp.returncode != 0:
            return []
        out: list[ListeningPort] = []
        for line in cp.stdout.splitlines():
            if "LISTEN" not in line and "LISTENING" not in line:
                continue
            parts = line.split()
            for tok in parts:
                if ":" not in tok:
                    continue
                host, port_s = tok.rsplit(":", 1)
                if not port_s.isdigit():
                    continue
                host = host if host not in {"*", "0.0.0.0"} else "0.0.0.0"
                try:
                    out.append(ListeningPort(port=int(port_s), address=host, proto="tcp"))
                except ValueError:
                    continue
        return out


# =====================================================================
#  HTML 预览本地服务
# =====================================================================

@dataclass
class PreviewHandle:
    """单次预览句柄。"""
    key: str
    url: str
    target_path: Path
    is_dir: bool
    expires_at: float
    port: int

    def expired(self) -> bool:
        return time.time() > self.expires_at


class HtmlPreviewServer:
    r"""本地静态预览服务器。

    用法：
        server = HtmlPreviewServer()
        server.start()
        handle = server.preview(r"D:\path\report.html", ttl_seconds=3600)
        print(handle.url)  # http://127.0.0.1:PORT/p/<key>/

    UI 层可以把 handle.url 塞进 QWebEngineView / 系统浏览器打开。
    """

    def __init__(self, bind_host: str = "127.0.0.1", bind_port: int = 0):
        self.bind_host = bind_host
        self.requested_port = bind_port
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._handles: dict[str, PreviewHandle] = {}
        self._lock = threading.RLock()

    # ------------- start/stop -------------
    def start(self) -> int:
        """启动服务器。返回实际绑定端口。"""
        if self._server is not None:
            return self._server.server_address[1]
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            # 屏蔽 BaseHTTPRequestHandler 自带 stderr 日志（干扰用户）
            def log_message(self, fmt, *args):  # noqa: N802
                return

            def do_GET(self):  # noqa: N802
                outer._serve(self)

            def do_HEAD(self):  # noqa: N802
                outer._serve(self, head_only=True)

        host = self.bind_host
        port = self.requested_port
        # 如果指定 0，让 OS 选一个自由端口
        for attempt in range(5):
            try:
                self._server = ThreadingHTTPServer((host, port), _Handler)
                break
            except OSError:
                port = 0  # 下次尝试 OS 分配
                time.sleep(0.05)
        if self._server is None:
            raise RuntimeError("无法绑定预览 HTTP 端口。")
        actual_port = self._server.server_address[1]
        t = threading.Thread(target=self._server.serve_forever, daemon=True,
                             name=f"html-preview:{actual_port}")
        t.start()
        self._server_thread = t
        log.info("HTML 预览服务已启动：http://%s:%d", host, actual_port)
        return actual_port

    def stop(self) -> None:
        srv = self._server
        if srv is None:
            return
        try:
            srv.shutdown()
            srv.server_close()
        except Exception as e:
            log.warning("关闭预览服务出错: %s", e)
        self._server = None
        self._server_thread = None

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        return self._server.server_address[1] if self._server else 0

    # ------------- preview 注册 -------------
    def preview(self, target: str | Path, ttl_seconds: int = 6 * 3600) -> PreviewHandle:
        """托管一个 HTML 文件/目录并返回预览句柄。"""
        if self._server is None:
            self.start()
        p = Path(target).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"预览目标不存在: {p}")
        is_dir = p.is_dir()
        if not is_dir and p.suffix.lower() not in {".html", ".htm", ".svg", ".md"}:
            # 单文件预览时，若不是 HTML/SVG/MD，也允许（作为静态资源托管，但浏览器不一定能直接渲染）
            log.info("预览非 HTML 文件：%s", p.suffix)
        key = uuid.uuid4().hex[:16]
        handle = PreviewHandle(
            key=key,
            url="",  # 下方补
            target_path=p,
            is_dir=is_dir,
            expires_at=time.time() + ttl_seconds,
            port=self.port,
        )
        handle.url = self._url_for(key, is_dir)
        with self._lock:
            self._handles[key] = handle
            self._gc_locked()
        log.info("注册预览 key=%s url=%s -> %s", key, handle.url, p)
        return handle

    def close_preview(self, key: str) -> None:
        with self._lock:
            self._handles.pop(key, None)

    def _url_for(self, key: str, is_dir: bool) -> str:
        port = self.port
        base = f"http://{self.bind_host}:{port}/p/{key}/"
        return base if is_dir else base + ""  # 文件/目录都走 key 根路径，服务端自行 resolve index.html

    # ------------- HTTP 服务核心 -------------
    def _serve(self, handler: BaseHTTPRequestHandler, head_only: bool = False) -> None:
        req_path = urllib.parse.urlparse(handler.path).path
        if not req_path.startswith("/p/"):
            # 根路径：返回健康 JSON
            body = json.dumps({"ok": True, "preview_count": len(self._handles)}).encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            if not head_only:
                handler.wfile.write(body)
            return
        # /p/<key>/<subpath...>
        tail = req_path[len("/p/"):]
        if "/" in tail:
            key, sub = tail.split("/", 1)
        else:
            key, sub = tail, ""
        sub = urllib.parse.unquote(sub)
        with self._lock:
            handle = self._handles.get(key)
            if handle and handle.expired():
                del self._handles[key]
                handle = None
        if handle is None:
            self._send_error(handler, 404, "预览链接无效或已过期。", head_only=head_only)
            return
        # 安全：禁止跳出 target_path（.. / 符号链接）
        if handle.is_dir:
            sub_clean = sub.lstrip("/")
            if sub_clean == "" or sub_clean.endswith("/"):
                sub_clean = (sub_clean + "index.html").lstrip("/")
            candidate = (handle.target_path / sub_clean).resolve()
            try:
                candidate.relative_to(handle.target_path.resolve())
            except ValueError:
                self._send_error(handler, 403, "路径越界。", head_only=head_only)
                return
            file_path = candidate
        else:
            file_path = handle.target_path
        if not file_path.exists() or not file_path.is_file():
            # 目录默认首页没找到：返回 404 带提示
            if handle.is_dir and (sub in ("", "/", "index.html")):
                self._send_dir_listing(handler, handle.target_path, head_only=head_only)
                return
            self._send_error(handler, 404, f"文件不存在：{file_path.name}", head_only=head_only)
            return
        # 读取 + 返回（大文件流式分块，避免一次性读入内存）
        try:
            file_size = file_path.stat().st_size
        except OSError as e:
            self._send_error(handler, 500, f"读取失败：{e}", head_only=head_only)
            return
        mime, _ = mimetypes.guess_type(str(file_path))
        if not mime:
            mime = "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/json", "application/javascript"}:
            mime = f"{mime}; charset=utf-8"
        handler.send_response(200)
        handler.send_header("Content-Type", mime)
        handler.send_header("Content-Length", str(file_size))
        # 禁止预览接口被作为 iframe 嵌入到未知来源（安全：仅同源）
        handler.send_header("X-Frame-Options", "SAMEORIGIN")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if not head_only:
            try:
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(64 * 1024)
                        if not chunk:
                            break
                        handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError as e:
                log.warning("流式发送预览文件失败 %s: %s", file_path, e)

    def _send_error(self, handler: BaseHTTPRequestHandler, code: int, msg: str,
                    head_only: bool) -> None:
        body = f"<h3>{code} Preview Error</h3><p>{msg}</p>".encode("utf-8")
        handler.send_response(code)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if not head_only:
            handler.wfile.write(body)

    def _send_dir_listing(self, handler: BaseHTTPRequestHandler, dir_path: Path,
                          head_only: bool) -> None:
        try:
            names = sorted([p.name for p in dir_path.iterdir()])
        except OSError:
            names = []
        html = ("<h3>Preview Directory</h3><ul>"
                + "".join(f'<li><a href="{urllib.parse.quote(n)}">{n}</a></li>' for n in names)
                + "</ul>").encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(html)))
        handler.end_headers()
        if not head_only:
            handler.wfile.write(html)

    def _gc_locked(self) -> None:
        now = time.time()
        expired = [k for k, h in self._handles.items() if h.expires_at < now]
        for k in expired:
            del self._handles[k]


# 便于只导入一次的全局单例
_port_manager: PortManager | None = None
_html_server: HtmlPreviewServer | None = None
_global_lock = threading.Lock()


def default_port_manager() -> PortManager:
    global _port_manager
    if _port_manager is None:
        with _global_lock:
            if _port_manager is None:
                _port_manager = PortManager()
    return _port_manager


def default_html_preview_server() -> HtmlPreviewServer:
    global _html_server
    if _html_server is None:
        with _global_lock:
            if _html_server is None:
                _html_server = HtmlPreviewServer()
                _html_server.start()
    return _html_server
