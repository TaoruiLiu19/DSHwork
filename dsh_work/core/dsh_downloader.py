"""DSH 便携运行时下载器（P3-2）。

目标：让用户无需预装 Node.js / npm 即可使用 DSH Work（"傻瓜式安装"）。

策略：
1. 下载官方便携 Node.js 到 ~/.dsh-work/runtime/node-<ver>-<platform>/
2. 用便携 Node 自带的 npm 把 @deepseek-ai/dsh 安装到 ~/.dsh-work/runtime/
3. 启动时用 [便携node, dsh入口js, --profile, web] 直接拉起，绕过系统 PATH

DSH 是纯 TypeScript CLI（npm 分发，含 node-pty 等原生模块，但走 N-API 跨版本兼容），
官方无独立二进制，因此"不依赖 npm"= 自带便携 Node 运行时 + 本地安装包。

所有步骤带行级日志回调，供启动画面诊断透传。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

from .. import constants as C
from ..config import get_runtime_dir
from ..utils.logger import get_logger

log = get_logger("core.dsh_downloader")

# 行级日志回调（与 ProcessManager._emit_log 同签名）
LogCb = Callable[[str], None]
# 结构化进度回调：(done_bytes, total_bytes)；total_bytes=0 表示未知大小（UI 用不确定模式）
ProgressCb = Callable[[int, int], None]


def _platform_tag() -> str:
    """返回 nodejs.org 分发文件名中的平台-架构标签，如 win-x64 / darwin-arm64 / linux-x64。"""
    s = platform.system()
    m = platform.machine().lower()
    if m in ("x86_64", "amd64", "x64"):
        arch = "x64"
    elif m in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        arch = m
    if s == "Windows":
        return f"win-{arch}"
    if s == "Darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def get_node_dir() -> Path:
    """便携 Node 解压目录（含 node 可执行文件与内置 npm）。"""
    return get_runtime_dir() / f"node-{C.PORTABLE_NODE_VERSION}-{_platform_tag()}"


def _node_archive_url() -> str:
    ver = C.PORTABLE_NODE_VERSION
    tag = _platform_tag()
    if tag.startswith("win-"):
        return f"{C.NODE_DIST_BASE}/{ver}/node-{ver}-{tag}.zip"
    if tag.startswith("darwin-"):
        return f"{C.NODE_DIST_BASE}/{ver}/node-{ver}-{tag}.tar.gz"
    return f"{C.NODE_DIST_BASE}/{ver}/node-{ver}-{tag}.tar.xz"


def get_node_bin() -> Path | None:
    """便携 node 可执行文件路径，未安装返回 None。"""
    d = get_node_dir()
    if not d.is_dir():
        return None
    exe = d / "node.exe" if os.name == "nt" else d / "bin" / "node"
    return exe if exe.is_file() else None


def get_npm_cli_js() -> Path | None:
    """便携包内置 npm 的 cli 入口 JS，未安装返回 None。"""
    d = get_node_dir()
    if not d.is_dir():
        return None
    p = d / "node_modules" / "npm" / "bin" / "npm-cli.js"
    return p if p.is_file() else None


def get_local_dsh_entry() -> Path | None:
    """本地安装的 dsh 入口 JS（读 package.json 的 bin 字段定位），未安装返回 None。"""
    pkg_dir = get_runtime_dir() / "node_modules" / C.DSH_CLI_PACKAGE
    pkg_json = pkg_dir / "package.json"
    if not pkg_json.is_file():
        return None
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
        bin_field = data.get("bin")
        target: str | None = None
        if isinstance(bin_field, str):
            target = bin_field
        elif isinstance(bin_field, dict):
            # 优先取与包名同名的入口，否则第一个
            target = bin_field.get(C.DSH_CLI_PACKAGE) or next(iter(bin_field.values()), None)
        if not target:
            return None
        entry = (pkg_dir / target).resolve()
        return entry if entry.is_file() else None
    except Exception as e:
        log.error("解析 dsh package.json 失败: %s", e)
        return None


def get_dsh_command() -> list[str] | None:
    """返回用本地便携运行时启动 dsh 的命令，未就绪返回 None。"""
    node = get_node_bin()
    if not node:
        return None
    entry = get_local_dsh_entry()
    if not entry:
        return None
    # 与 ProcessManager.start_dsh 保持一致：--profile web（不加 --workspace）
    return [str(node), str(entry), "--profile", "web"]


def is_runtime_ready() -> bool:
    """本地运行时是否就绪（便携 node + 本地 dsh 均存在）。"""
    return get_dsh_command() is not None


# ===== 下载 =====


def _download(url: str, dest: Path, log_cb: LogCb, timeout: int,
              progress_cb: ProgressCb | None = None) -> bool:
    """流式下载并显示进度，下载到 .part 再原子重命名。

    progress_cb(done_bytes, total_bytes)：每写入一个 chunk 调用一次，
    total_bytes=0 表示响应头未返回 Content-Length（UI 用不确定模式）。
    """
    import requests

    log_cb(f"下载: {url}")
    try:
        resp = requests.get(url, stream=True, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        log_cb(f"⚠ 下载失败: {e}")
        return False
    total = int(resp.headers.get("Content-Length", 0))
    tmp = dest.with_suffix(dest.suffix + ".part")
    done = 0
    last_pct = -1
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    # 结构化进度回调（UI 端可自行节流）
                    if progress_cb:
                        try:
                            progress_cb(done, total)
                        except Exception:
                            pass
                    # 日志：每 10% 打印一次，避免日志框刷屏
                    if total:
                        pct = done * 100 // total
                        if pct != last_pct and pct % 10 == 0:
                            log_cb(
                                f"  {pct}% ({done // 1024 // 1024}MB / {total // 1024 // 1024}MB)"
                            )
                            last_pct = pct
        tmp.replace(dest)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        log_cb(f"⚠ 写入失败: {e}")
        return False
    log_cb(f"下载完成: {dest.name}")
    return True


def _extract(archive: Path, dest_dir: Path, log_cb: LogCb) -> bool:
    log_cb(f"解压: {archive.name} -> {dest_dir.name}")
    try:
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest_dir)
        else:
            mode = "r:gz" if archive.name.endswith(".tar.gz") else "r:xz"
            with tarfile.open(archive, mode) as tf:
                tf.extractall(dest_dir)
    except Exception as e:
        log_cb(f"⚠ 解压失败: {e}")
        return False
    log_cb("解压完成")
    return True


def _flatten(node_dir: Path, log_cb: LogCb) -> None:
    """官方便携包解压后是单层子目录 node-<ver>-<platform>/，把内容上移到 node_dir 根。"""
    children = [p for p in node_dir.iterdir()]
    if len(children) == 1 and children[0].is_dir():
        sub = children[0]
        for item in sub.iterdir():
            shutil.move(str(item), str(node_dir / item.name))
        sub.rmdir()
        log_cb(f"已展平 {sub.name} 到运行时根")


def download_portable_node(log_cb: LogCb,
                           progress_cb: ProgressCb | None = None) -> Path | None:
    """下载并解压便携 Node.js，返回 node 可执行文件路径，失败返回 None。"""
    if get_node_bin():
        log_cb(f"便携 Node 已存在: {get_node_dir()}")
        return get_node_bin()

    node_dir = get_node_dir()
    node_dir.mkdir(parents=True, exist_ok=True)
    url = _node_archive_url()

    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / Path(url).name
        if not _download(url, archive, log_cb, C.NODE_DOWNLOAD_TIMEOUT_SEC, progress_cb):
            return None
        if not _extract(archive, node_dir, log_cb):
            return None
        _flatten(node_dir, log_cb)

    node = get_node_bin()
    if not node:
        log_cb("⚠ 便携 Node 解压后未找到 node 可执行文件")
        return None
    # 验证可运行
    try:
        r = subprocess.run(
            [str(node), "--version"], capture_output=True, text=True, timeout=15, shell=False
        )
        if r.returncode == 0:
            log_cb(f"便携 Node 就绪: {r.stdout.strip()}")
            return node
        log_cb(f"⚠ 便携 Node 启动失败 rc={r.returncode} {r.stderr.strip()}")
    except Exception as e:
        log_cb(f"⚠ 便携 Node 验证异常: {e}")
    return None


# ===== 本地安装 dsh =====


def install_dsh_local(log_cb: LogCb) -> bool:
    """用便携 Node 的 npm 把 @deepseek-ai/dsh 装到 runtime 目录。"""
    node = get_node_bin()
    if not node:
        log_cb("⚠ 无便携 Node，无法安装 dsh（请先下载便携运行时）")
        return False
    if get_local_dsh_entry():
        log_cb("本地 dsh 已安装，跳过")
        return True
    npm_js = get_npm_cli_js()
    if not npm_js:
        log_cb("⚠ 便携包未内置 npm，无法本地安装 dsh")
        return False

    runtime = get_runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    log_cb(f"npm install {C.DSH_CLI_PACKAGE} -> {runtime}")
    log_cb(f"（超时 {C.DSH_LOCAL_INSTALL_TIMEOUT_SEC}s，使用国内镜像加速，请耐心等待...）")

    cmd = [
        str(node), str(npm_js), "install", C.DSH_CLI_PACKAGE,
        "--prefix", str(runtime),
        "--registry", C.NPM_REGISTRY_MIRROR,
        "--no-audit", "--no-fund", "--loglevel=error",
    ]

    is_windows = os.name == "nt"
    creationflags = 0
    if is_windows:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            shell=False,
            creationflags=creationflags,
        )
    except Exception as e:
        log_cb(f"⚠ 启动 npm 失败: {e}")
        return False

    import time

    deadline = time.time() + C.DSH_LOCAL_INSTALL_TIMEOUT_SEC
    timed_out = False
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue
            line = line.rstrip()
            if line:
                log_cb(line)
            if time.time() > deadline:
                timed_out = True
                break
    except Exception as e:
        log_cb(f"⚠ 读取 npm 输出异常: {e}")

    if timed_out or (proc.poll() is None and time.time() > deadline):
        log_cb(f"⚠ 安装超时（{C.DSH_LOCAL_INSTALL_TIMEOUT_SEC}s），终止 npm 进程树")
        _kill_tree(proc)
        return False

    try:
        rc = proc.wait(timeout=5)
    except Exception:
        _kill_tree(proc)
        rc = -1

    if rc != 0:
        log_cb(f"⚠ npm install 失败，退出码: {rc}")
        return False

    entry = get_local_dsh_entry()
    if not entry:
        log_cb("⚠ npm 安装完成但未找到 dsh 入口（包结构异常？）")
        return False
    log_cb(f"本地 dsh 安装成功: {entry.parent.name}")
    return True


def _kill_tree(proc: subprocess.Popen) -> None:
    """终止进程树（Windows 用 taskkill /T）。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5, shell=False,
            )
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def ensure_runtime(log_cb: LogCb,
                   progress_cb: ProgressCb | None = None) -> list[str] | None:
    """确保本地运行时就绪（缺则下载/安装），返回 dsh 启动命令或 None。"""
    cmd = get_dsh_command()
    if cmd:
        log_cb("本地运行时就绪")
        return cmd
    if not get_node_bin():
        if not download_portable_node(log_cb, progress_cb):
            return None
    if not get_local_dsh_entry():
        if not install_dsh_local(log_cb):
            return None
    return get_dsh_command()
