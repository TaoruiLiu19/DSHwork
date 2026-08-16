"""DSH 进程管理层（第 2.4 节）。

进程所有权与 PID 文件锁：
- 启动 DSH 子进程时，在系统临时目录用 portalocker 写入 .dsh.pid
- 新实例启动时除端口探测外，还需校验该端口对应进程的 PID 是否与 .dsh.pid 一致

环境检测（第 7.2 节）：
- Node.js 检测：node --version
- DSH CLI 检测：npx @deepseek-ai/dsh --version
- DSH 运行中检测：版本适配器裸协议探测
- API Key 检测：credentials.describe
- 可用模型：session.models

首次启动超时的诊断透传：
- 启动画面下方实时日志输出框（高度约 80px），捕获 DSH 子进程 stderr/stdout
- 超时后提供"一键导出诊断日志"按钮
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .. import constants as C
from ..utils.logger import get_logger
from ..utils.pid_lock import PidLock, PidInfo

log = get_logger("core.process_manager")


def _resolve_cmd(command: str) -> list[str]:
    """Windows 下优先找到 .cmd/.exe 绝对路径。

    例如传入 'npm' → 返回 ['<PATH>/npm.cmd']。
    Unix 下直接返回原命令（列表形式）。
    """
    if os.name != "nt":
        return [command]
    try:
        result = subprocess.run(
            ["where", command], capture_output=True, text=True, timeout=3, shell=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                # 优先 .cmd (NPM), 其次 .exe, 其他可接受
                low = line.lower()
                if low.endswith(".cmd"):
                    return [line]
                if low.endswith(".exe"):
                    return [line]
            # 没 .cmd/.exe 就取第一个
            first = result.stdout.strip().splitlines()[0].strip()
            return [first]
    except Exception:
        pass
    # 找不到则回退 shell=True（在调用方单独处理）
    return [command]


def _can_shell_false(path: str) -> bool:
    """Windows 下只有 .exe 文件才能 shell=False 直接启动。

    .cmd/.bat 是批处理脚本，CreateProcess 不能直接执行，必须通过 cmd.exe /c 解释。
    所以对 .cmd/.bat 文件保持 shell=True（我们已有 _kill_process_tree + taskkill /T
    解决 shell=True 的超时杀不净问题）。
    """
    return path.lower().endswith(".exe")


# 这些路径片段是 IDE/Agent 沙箱自带的 node/dsh 捆绑包（不是用户真实安装的），
# 它们依赖特定的启动环境，直接独立启动会哑掉，必须在解析时跳过。
_SANDBOX_PATH_TOKENS = (
    "trae solo",
    "traesolo",
    "modulardata",
    "ai-agent",
    "vm\\tools\\node",
    "vm/tools/node",
)


def _is_sandboxed_dsh(path: str) -> bool:
    """判断给定 dsh 路径是否是 IDE/Agent 沙箱捆绑的（不可独立使用）。"""
    if not path:
        return False
    low = path.lower().replace("/", "\\")
    return any(tok.replace("/", "\\") in low for tok in _SANDBOX_PATH_TOKENS)


def _find_user_dsh_path() -> str | None:
    """找到用户自己安装的 DSH CLI 路径，**永远优先**于沙箱捆绑。

    查找顺序：
      1. `npm prefix -g` 返回的全局目录下的 dsh.CMD（Windows） / dsh（Unix）
      2. `where/which dsh` 返回的所有命中中，去掉沙箱命中，取第一个

    沙箱环境（如 TRAE Agent shell）会把自己的 node/dsh 路径放在 PATH 最前，
    导致 `shutil.which` / `where dsh` 直接命中沙箱版而完全跳过用户真实安装。
    用 `npm prefix -g` 直接定位到用户 npm 全局目录从根源避免此问题。
    """
    import shutil

    # 0. 硬编码标准位置（优先级最高）——沙箱再怎么劫持 PATH 也不影响这里
    #    Windows: %APPDATA%\npm   (C:\Users\<user>\AppData\Roaming\npm)
    #    Unix/Mac: ~/.npm-global/bin 或 /usr/local/bin
    hardcoded_prefixes: list[str] = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expandvars(r"%USERPROFILE%\AppData\Roaming")
        hardcoded_prefixes.append(os.path.join(appdata, "npm"))
    else:
        home = os.path.expanduser("~")
        hardcoded_prefixes.append(os.path.join(home, ".npm-global", "bin"))
        hardcoded_prefixes.append("/usr/local/bin")
    for hp in hardcoded_prefixes:
        if os.name == "nt":
            for name in ("dsh.CMD", "dsh.cmd", "dsh.exe", "dsh"):
                p = os.path.join(hp, name)
                if os.path.isfile(p) and not _is_sandboxed_dsh(p):
                    log.debug("通过标准路径定位到 dsh: %s", p)
                    return p
        else:
            p = os.path.join(hp, "dsh")
            if os.path.isfile(p) and not _is_sandboxed_dsh(p):
                log.debug("通过标准路径定位到 dsh: %s", p)
                return p

    # 1. 再查 npm prefix -g —— 注意：沙箱环境会返回沙箱 npm，需要继续校验
    try:
        result = _run_subprocess_safe(
            ["npm", "prefix", "-g"], timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            prefix = result.stdout.strip()
            candidates = []
            if os.name == "nt":
                candidates.extend([
                    os.path.join(prefix, "dsh.CMD"),
                    os.path.join(prefix, "dsh.cmd"),
                    os.path.join(prefix, "dsh.exe"),
                    os.path.join(prefix, "dsh"),
                ])
            else:
                candidates.append(os.path.join(prefix, "bin", "dsh"))
            for p in candidates:
                if os.path.isfile(p) and not _is_sandboxed_dsh(p):
                    log.debug("通过 npm prefix 定位到 dsh: %s", p)
                    return p
    except Exception as e:
        log.debug("npm prefix 查寻失败(不致命): %s", e)

    # 2. 枚举 where/which 的所有命中，跳过沙箱版
    try:
        where_cmd = "where" if os.name == "nt" else "which"
        where_rs = _run_subprocess_safe(
            [where_cmd, C.DSH_CLI_COMMAND], timeout=3,
        )
        if where_rs.returncode == 0 and where_rs.stdout.strip():
            for line in where_rs.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                if os.path.isfile(line) and not _is_sandboxed_dsh(line):
                    log.debug("通过 where 枚举定位到 dsh: %s", line)
                    return line
    except Exception:
        pass

    # 3. 最后兜底：shutil.which，如果命中沙箱版仍然放弃
    path = shutil.which(C.DSH_CLI_COMMAND)
    if path and os.path.isfile(path) and not _is_sandboxed_dsh(path):
        log.debug("通过 shutil.which 定位到 dsh: %s", path)
        return path

    log.warning("所有定位方法都没找到用户 DSH CLI（可能未安装或处于沙箱环境）")
    return None


def _run_subprocess_safe(args: list[str], *, timeout: int, capture_output: bool = True,
                         text: bool = True, shell: bool = True, cwd: str | None = None,
                         env: dict | None = None) -> subprocess.CompletedProcess:
    """安全运行子进程，确保 Windows 下能正确终止整个进程树。

    优先策略（Windows）：
      1. 用 _resolve_cmd 找 args[0] 的 .cmd/.exe 绝对路径
      2. shell=False 直接启动（进程树简单、无 PowerShell 执行策略、taskkill 干净）
      3. 解析失败时回退到 shell=True

    Windows 上 shell=True 时 subprocess.run 的 timeout 只会杀 cmd.exe，
    不会杀其子进程（如 npx → node.exe），导致子进程持有管道而 Python 无限阻塞。
    解决方案：用 Popen + 独立 watchdog 线程，超时后用 taskkill /T /F 杀整个进程树。
    """
    is_windows = os.name == "nt"
    use_shell = shell
    resolved_args = list(args)

    if is_windows and args:
        resolved = _resolve_cmd(args[0])
        # 只有 .exe 文件才能 shell=False（.cmd/.bat 需要 cmd.exe 解释，必须 shell=True）
        if (resolved and resolved[0] != args[0]
                and os.path.isabs(resolved[0])
                and _can_shell_false(resolved[0])):
            resolved_args = [resolved[0]] + list(args[1:])
            use_shell = False

    creationflags = 0
    if is_windows:
        # 新进程组：让 taskkill 可以连带杀子进程
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )

    proc = subprocess.Popen(
        resolved_args,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=text,
        shell=use_shell,
        cwd=cwd,
        env=env,
        creationflags=creationflags,
    )

    deadline = time.time() + timeout
    stdout_buf: list[str] = []
    stderr_buf: list[str] = []

    def _reader(stream, buf):
        try:
            for line in stream:
                buf.append(line)
        except Exception:
            pass

    threads = []
    if capture_output and proc.stdout:
        t1 = threading.Thread(target=_reader, args=(proc.stdout, stdout_buf), daemon=True)
        t1.start()
        threads.append(t1)
    if capture_output and proc.stderr:
        t2 = threading.Thread(target=_reader, args=(proc.stderr, stderr_buf), daemon=True)
        t2.start()
        threads.append(t2)

    timed_out = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    else:
        # 超时：强制杀进程树
        timed_out = True
        if is_windows:
            try:
                # Windows: 用 taskkill /T 连带子进程一起杀
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=3, shell=False,
                )
            except Exception:
                pass
        else:
            try:
                # Unix: 发 SIGTERM 给进程组
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass

    for t in threads:
        # 等 reader 线程读到 EOF（stream 关闭后 for line 会自然退出）
        # 给 2s 足够时间，避免进程极快退出时 reader 还没读完输出
        t.join(timeout=2)

    returncode = proc.returncode if proc.returncode is not None else -1
    stdout = "".join(stdout_buf) if capture_output else ""
    stderr = "".join(stderr_buf) if capture_output else ""

    if timed_out:
        raise subprocess.TimeoutExpired(
            cmd=resolved_args, timeout=timeout,
            output=stdout if capture_output else None,
            stderr=stderr if capture_output else None,
        )
    return subprocess.CompletedProcess(
        args=resolved_args, returncode=returncode, stdout=stdout, stderr=stderr
    )


class ProcessOwnership(str, Enum):
    """DSH 进程所有权状态。"""

    OWNED = "owned"          # 本客户端启动，退出时可安全终止
    EXTERNAL = "external"    # 用户手动启动，仅附加连接
    STALE = "stale"          # PID 文件存在但进程已僵死
    PORT_CONFLICT = "conflict"  # 端口被占但无 PID 文件
    NOT_RUNNING = "not_running"  # DSH 未运行


@dataclass
class EnvironmentCheck:
    """环境检测结果（第 7.2 节）。"""

    node_ok: bool = False
    node_version: str = ""
    dsh_cli_ok: bool = False
    dsh_cli_version: str = ""
    dsh_running: bool = False
    dsh_port: int = C.DSH_DEFAULT_PORT
    api_key_ok: bool = False
    models_available: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)  # 实时诊断日志透传
    # 首次启动需要下载 DSH（阻塞式下载面板用）
    need_download: bool = False
    download_type: str = ""  # "local_runtime"（便携运行时）/ "npm_global"（npm install -g）

    @property
    def all_ok(self) -> bool:
        return (
            self.node_ok
            and self.dsh_cli_ok
            and self.dsh_running
            and self.api_key_ok
            and len(self.models_available) > 0
        )


class ProcessManager:
    """DSH 进程管理器。

    负责：
    1. 环境检测（Node.js / DSH CLI / DSH 运行中 / API Key / 可用模型）
    2. DSH 子进程启动与生命周期管理
    3. PID 文件锁所有权校验
    4. 子进程 stderr/stdout 实时透传（诊断日志）
    """

    def __init__(self):
        self.pid_lock = PidLock()
        self._dsh_process: subprocess.Popen | None = None
        self._log_callback: Callable[[str], None] | None = None
        self._ownership: ProcessOwnership = ProcessOwnership.NOT_RUNNING

    @property
    def ownership(self) -> ProcessOwnership:
        return self._ownership

    @property
    def is_dsh_owned(self) -> bool:
        """本客户端是否拥有 DSH 进程所有权。"""
        return self._ownership == ProcessOwnership.OWNED

    def set_log_callback(self, callback: Callable[[str], None]) -> None:
        """设置实时日志回调（启动画面诊断透传用）。"""
        self._log_callback = callback

    def _emit_log(self, line: str) -> None:
        """输出诊断日志到回调和 logger。"""
        line = line.rstrip()
        if not line:
            return
        if self._log_callback:
            self._log_callback(line)
        log.info("[dsh] %s", line)

    # ===== 环境检测 =====

    def check_environment(self) -> EnvironmentCheck:
        """执行环境检测（第 7.2 节）。

        检测内容按优先级排列：
        1. Node.js（node --version）
        2. DSH CLI（npx @deepseek-ai/dsh --version）
        3. DSH 运行中（版本适配器裸协议探测）
        4. API Key（credentials.describe）
        5. 可用模型（session.models）
        """
        check = EnvironmentCheck()

        # 1. Node.js 检测
        check.node_ok, check.node_version = self._check_node()
        if not check.node_ok:
            check.errors.append(
                f"未检测到 Node.js {C.NODE_MIN_VERSION_MAJOR}+。"
                f"可安装 Node.js（https://nodejs.org/），或在启动画面点击"
                f"\"一键安装便携运行时\"由 DSH Work 自动下载（无需系统 Node/npm）。"
            )

        # 2. DSH CLI 检测（含本地便携运行时）
        check.dsh_cli_ok, check.dsh_cli_version = self._check_dsh_cli()
        if not check.dsh_cli_ok:
            check.errors.append(
                f"未检测到 DSH CLI。可运行 npm install -g {C.DSH_CLI_PACKAGE}，"
                f"或在启动画面点击\"一键安装便携运行时\"自动下载（不依赖系统 npm）。"
            )

        # 3. DSH 运行中检测 + 所有权校验
        check.dsh_running = self._check_dsh_running()
        check.dsh_port = C.DSH_DEFAULT_PORT

        # 4 & 5. API Key + 可用模型（仅在 DSH 运行时检测）
        if check.dsh_running:
            try:
                from ..api import DshService
                dsh = DshService()
                probe = dsh.initialize()
                if probe.success:
                    creds = dsh.check_credentials()
                    check.api_key_ok = bool(creds.get("has_key") or creds.get("valid"))
                    if not check.api_key_ok:
                        check.errors.append("API Key 未配置或无效，请在设置中输入")
                    models = dsh.get_models()
                    check.models_available = [m.id for m in models]
                    if not check.models_available and check.api_key_ok:
                        check.errors.append("无可用模型，请检查 API Key 权限")
                    dsh.shutdown()
            except Exception as e:
                check.errors.append(f"DSH 通信检测失败: {e}")
                log.error("环境检测通信异常: %s", e)

        return check

    def _check_node(self) -> tuple[bool, str]:
        """检测 Node.js 版本。"""
        try:
            result = _run_subprocess_safe(
                ["node", "--version"], timeout=10,
            )
            if result.returncode != 0:
                return False, ""
            version_str = result.stdout.strip()  # 如 "v18.17.0"
            match = re.match(r"v?(\d+)\.", version_str)
            if not match:
                return False, version_str
            major = int(match.group(1))
            if major < C.NODE_MIN_VERSION_MAJOR:
                return False, version_str
            return True, version_str
        except (subprocess.SubprocessError, FileNotFoundError):
            return False, ""

    def _check_dsh_cli(self) -> tuple[bool, str]:
        """检测 DSH CLI。

        检测优先级：
        1. _find_user_dsh_path() — 直接定位用户 npm 全局版本，避开沙箱捆绑
        2. 本地便携运行时（P3-2，不依赖系统 npm）
        3. npx（超时 5s，首次可能需下载包）
        """
        # 1. 优先使用 _find_user_dsh_path 精确找用户自己的 DSH
        dsh_path = _find_user_dsh_path()
        if dsh_path:
            try:
                ver_result = _run_subprocess_safe(
                    [dsh_path, "--version"], timeout=5,
                )
                if ver_result.returncode == 0:
                    return True, ver_result.stdout.strip()
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
            return True, "installed"

        # 2. 本地便携运行时（P3-2，不依赖系统 npm）
        from . import dsh_downloader
        if dsh_downloader.is_runtime_ready():
            return True, "local-runtime"

        # 3. 回退：npx 检测（超时 5s）
        try:
            result = _run_subprocess_safe(
                ["npx", C.DSH_CLI_PACKAGE, "--version"], timeout=5,
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            return False, ""
        except (subprocess.SubprocessError, FileNotFoundError):
            return False, ""

    def install_dsh_cli(
        self, timeout: int = C.DSH_CLI_INSTALL_TIMEOUT_SEC
    ) -> bool:
        """自动安装 DSH CLI（npm install -g @deepseek-ai/dsh）。

        逐行读取 npm 输出并实时回显到启动画面日志框。
        Windows 下优先解析 npm.cmd 绝对路径 + shell=False 启动，
        避免 shell=True 的 PowerShell 执行策略拦截和超时杀不净问题。

        Returns:
            True 表示安装成功（npm 退出码 0）
        """
        self._emit_log("正在执行: npm install -g @deepseek-ai/dsh")
        self._emit_log(f"（超时 {timeout}s，使用国内镜像加速，请耐心等待...）")

        is_windows = os.name == "nt"
        creationflags = 0
        if is_windows:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )

        # Windows: 解析 npm.cmd 绝对路径后 shell=False 启动，简化进程树
        # 添加 --registry 国内镜像加速（首次安装 530+ 包，默认源可能超时）
        cmd_args = [
            "npm", "install", "-g", C.DSH_CLI_PACKAGE,
            "--registry", C.NPM_REGISTRY_MIRROR,
        ]
        use_shell = True
        if is_windows:
            resolved = _resolve_cmd("npm")
            if (resolved and os.path.isabs(resolved[0])
                    and _can_shell_false(resolved[0])):
                cmd_args = [resolved[0]] + cmd_args[1:]
                use_shell = False

        try:
            proc = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                shell=use_shell,
                creationflags=creationflags,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            self._emit_log(f"启动 npm 失败: {e}")
            return False

        deadline = time.time() + timeout
        timed_out = False

        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                    continue
                # 对每一行去重 \n 再回显，避免日志框空行
                line = line.rstrip()
                if line:
                    self._emit_log(line)
                if time.time() > deadline:
                    timed_out = True
                    break
        except Exception as e:
            self._emit_log(f"读取 npm 输出异常: {e}")

        if timed_out or (proc.poll() is None and time.time() > deadline):
            self._emit_log(f"安装超时（{timeout}s），终止 npm 进程树...")
            self._kill_process_tree(proc)
            return False

        try:
            rc = proc.wait(timeout=5)
        except Exception:
            self._kill_process_tree(proc)
            rc = -1
        if rc == 0:
            self._emit_log("DSH CLI 安装成功")
            # 关键修复：安装成功后刷新本进程的 PATH，让 shutil.which('dsh') 同一次运行内立即命中
            # （否则当前 Python 进程仍是启动时的 PATH 快照，下次重启才会生效）
            self._refresh_path_after_install()
            return True
        else:
            self._emit_log(f"npm install 失败，退出码: {rc}")
            return False

    def _refresh_path_after_install(self) -> None:
        """npm -g 安装后，把 npm prefix bin 目录加到当前进程 PATH。

        跨终端环境（TRAE 沙箱 vs 外部 PS）全局目录可能不同，需要实时
        刷新，避免下次 `shutil.which('dsh')` 仍然找不到，从而重复 npm install。
        """
        try:
            # npm prefix -g → <prefix>，Windows 上 .cmd 在 <prefix> 下
            result = _run_subprocess_safe(
                ["npm", "prefix", "-g"], timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return
            prefix = result.stdout.strip()
            # Windows: dsh.cmd 直接在 prefix 目录
            bin_dirs = [prefix]
            # Unix/Mac: <prefix>/bin
            if os.name != "nt":
                bin_dirs.insert(0, os.path.join(prefix, "bin"))
            path_var = os.environ.get("PATH", "")
            sep = ";" if os.name == "nt" else ":"
            existing_dirs = set(path_var.split(sep))
            added = []
            for d in bin_dirs:
                if os.path.isdir(d) and d not in existing_dirs:
                    added.append(d)
            if added:
                os.environ["PATH"] = sep.join(added + [path_var] if path_var else added)
                log.info("已将 npm 全局目录加入 PATH: %s", added)
        except Exception as e:
            log.debug("刷新 PATH 失败(不致命): %s", e)

    def ensure_local_runtime(self, progress_cb=None) -> bool:
        """一键安装便携运行时（P3-2）：下载便携 Node + 本地安装 dsh，不依赖系统 npm。

        供启动画面"一键安装便携运行时"按钮调用。诊断日志通过 _emit_log 实时回显。
        成功后 start_dsh 会自动用本地运行时拉起 DSH，无需系统 Node/npm。

        Args:
            progress_cb: 可选的下载进度回调 (done_bytes, total_bytes)，
                         total_bytes=0 表示未知大小。仅下载 Node 阶段会调用，
                         npm install 阶段无精确进度（UI 用不确定模式）。

        Returns:
            True 表示运行时就绪（get_dsh_command() 可用）
        """
        from . import dsh_downloader
        cmd = dsh_downloader.ensure_runtime(self._emit_log, progress_cb)
        if cmd:
            self._emit_log(f"便携运行时就绪: {cmd[0]}")
            return True
        self._emit_log("⚠ 便携运行时安装失败，详见上方日志")
        return False

    def _check_dsh_running(self) -> bool:
        """检测 DSH 是否正在运行 + 所有权校验。"""
        import socket

        # TCP 端口探测
        try:
            with socket.create_connection(
                (C.DSH_DEFAULT_HOST, C.DSH_DEFAULT_PORT), timeout=2
            ):
                pass
        except OSError:
            self._ownership = ProcessOwnership.NOT_RUNNING
            return False

        # 端口被占，校验 PID 所有权
        pid_info = self.pid_lock.read()
        if pid_info is None:
            # 端口被占但无 PID 文件 → 上次崩溃残留的孤儿进程
            # （正常退出时 stop_dsh 会写 PID 文件并清理端口；无 PID 文件说明
            #  上次进程未走完清理流程就退出了，残留的 node/dsh 仍占用 3080。）
            # 复用这种孤儿会导致本次启动在 init 阶段异常退出（WebSocket/会话状态不一致），
            # 因此统一清理孤儿，由后续 start_dsh 启动全新 DSH，保证本次启动稳定。
            self._ownership = ProcessOwnership.NOT_RUNNING
            log.warning(
                "端口 %d 被占但无 PID 文件，判定为上次崩溃残留孤儿，正在清理...",
                C.DSH_DEFAULT_PORT,
            )
            killed = self._kill_port_owner(C.DSH_DEFAULT_PORT)
            if killed:
                self._emit_log(
                    f"已清理 {killed} 个占用端口 {C.DSH_DEFAULT_PORT} 的孤儿进程（上次崩溃残留）"
                )
                # 清理后短暂等待端口释放（OS 回收 TIME_WAIT）
                import time as _t
                _t.sleep(0.5)
            return False

        if self.pid_lock.is_dsh_alive():
            if pid_info.owner == "dsh-work":
                self._ownership = ProcessOwnership.OWNED
                log.info("DSH 正在运行（本客户端启动）PID=%d", pid_info.pid)
            else:
                self._ownership = ProcessOwnership.EXTERNAL
                log.info("DSH 正在运行（用户手动启动）PID=%d", pid_info.pid)
            return True
        else:
            # PID 文件存在但进程已僵死
            self._ownership = ProcessOwnership.STALE
            log.warning("DSH 进程已僵死 PID=%d，将清理并重启", pid_info.pid)
            self.pid_lock.kill_stale()
            return False

    # ===== DSH 进程启动 =====

    def start_dsh(self, workspace: str = "", timeout: int = C.DSH_STARTUP_TIMEOUT_SEC) -> bool:
        """启动 DSH 子进程。

        启动优先级：
          1. 端口 3080 已被占（已有 DSH 在跑）→ 直接复用，不启动新进程
          2. 优先用已全局安装的 `dsh web`（最快，npx 前置开销 0）
          3. shutil.which('dsh') 找不到时才回退到 `npx @deepseek-ai/dsh web`

        同时：
        - 在系统临时目录写入 .dsh.pid（记录 PID 与监听端口）
        - 捕获 stderr/stdout 流，实时回显到启动画面
        - 解析 "dsh web: http://host:port" 行，把实际端口用于就绪检测（比写死 3080 更准）
        - 超时后用 taskkill /T 清理子进程树，避免挂起的 npx/node
        """
        import shutil

        # 1. 先检查端口是否已被占（已有 DSH 在跑 → 直接复用）
        if self._check_dsh_running():
            log.info("DSH 已在运行，跳过启动")
            return True

        # 清理僵死进程
        if self._ownership == ProcessOwnership.STALE:
            self.pid_lock.kill_stale()

        self._emit_log(f"正在启动 DSH... (workspace={workspace or 'default'})")

        # 1. 首选：用户全局安装的 dsh web（避开沙箱捆绑路径）
        dsh_path = _find_user_dsh_path()
        is_windows = os.name == "nt"
        creationflags = 0
        if is_windows:
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )

        if dsh_path and os.path.isabs(dsh_path):
            # .exe 文件可 shell=False 直接启动；.cmd/.bat 必须 shell=True（CreateProcess 不能直接执行批处理）
            #
            # 注意：DSH 0.1.0-rc.6 官方形式是 `dsh --profile web`，
            # 不要加 --workspace（没有这个选项！加了 DSH 会直接报错退出 unknown option）。
            # 工作区目录通过 subprocess 的 cwd 参数传入，DSH 会自动把当前目录当 workspace。
            cmd = [dsh_path, "--profile", "web"]
            use_shell = not _can_shell_false(dsh_path)
            self._emit_log(f"使用已安装的 DSH CLI: {dsh_path}")
        else:
            # 回退1：本地便携运行时（P3-2，不依赖系统 npm）
            from . import dsh_downloader
            local_cmd = dsh_downloader.get_dsh_command()
            if local_cmd:
                cmd = local_cmd
                use_shell = False  # [node.exe, entry.js, ...] 直接 shell=False，进程树最干净
                self._emit_log(f"使用本地便携运行时启动 DSH: {local_cmd[0]}")
            else:
                # 回退2：npx（首次可能下载包，更慢）
                cmd = ["npx", C.DSH_CLI_PACKAGE, "--profile", "web"]
                use_shell = True
                if is_windows:
                    resolved = _resolve_cmd("npx")
                    if (resolved and os.path.isabs(resolved[0])
                            and _can_shell_false(resolved[0])):
                        cmd = [resolved[0]] + cmd[1:]
                        use_shell = False
                self._emit_log("未找到全局 dsh 与本地运行时，使用 npx 启动（首次可能下载依赖，需耐心）")

        # 记录 use_shell 供 _wait_for_port 判断是否信任 proc.poll()
        self._dsh_use_shell = use_shell

        self._emit_log(f"准备启动 DSH: cmd={cmd} shell={use_shell} cwd={workspace} flags={creationflags}")

        try:
            self._dsh_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                shell=use_shell,
                cwd=workspace or None,
                creationflags=creationflags,
            )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            self._emit_log(f"启动 DSH 失败: {e}")
            return False

        self._emit_log(f"DSH 子进程已创建 PID={self._dsh_process.pid}")

        # 重置就绪信号
        self._dsh_ready_port = None

        # 写入 PID 文件锁（初始按默认端口写，后续读到 "dsh web: http://...:port" 会再更新）
        try:
            self.pid_lock.acquire(pid=self._dsh_process.pid, port=C.DSH_DEFAULT_PORT)
        except Exception as e:
            self._emit_log(f"PID 锁写入失败(不致命): {e}")
        self._ownership = ProcessOwnership.OWNED

        # 启动日志透传线程（保存引用以便 stop 时 join，避免子线程仍在跑导致退出告警）
        self._log_thread = threading.Thread(
            target=self._stream_logs,
            daemon=True,
            name="dsh-stdout",
        )
        self._log_thread.start()

        self._emit_log(f"开始等待端口就绪 (timeout={timeout}s)")
        # 等待端口就绪；失败则杀进程树，避免留下挂起的 npx/node
        ready = self._wait_for_port(timeout)
        self._emit_log(f"等待端口就绪结束: ready={ready}")
        # 最后兜底：有些时候（沙箱 shell=True + stdout=PIPE）DSH 内部启动极慢，
        # 即使 wait_for_port 超时判定失败，真实 DSH 也可能就在几秒后起来了。
        # 杀进程之前做最后一次探测，避免冤枉它。
        if not ready:
            import socket
            try:
                with socket.create_connection(
                    (C.DSH_DEFAULT_HOST, C.DSH_DEFAULT_PORT), timeout=2
                ):
                    ready = True
                    self._emit_log(
                        f"DSH 最终确认就绪 (http://{C.DSH_DEFAULT_HOST}:{C.DSH_DEFAULT_PORT})"
                        "（wait_for_port 超时后兜底检查命中）"
                    )
            except OSError:
                pass
        if not ready:
            self._emit_log("DSH 启动未就绪，清理子进程树")
            self._kill_process_tree(self._dsh_process)
            self._dsh_process = None
            self._ownership = ProcessOwnership.NOT_RUNNING
            try:
                self.pid_lock.release()
            except Exception:
                pass
        return ready

    def _kill_process_tree(self, proc: subprocess.Popen | None) -> None:
        """强制终止进程及其所有子进程（Windows / Unix 通用）。"""
        if proc is None:
            return
        try:
            if proc.poll() is not None:
                return  # 已退出
        except Exception:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5, shell=False,
                )
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass

    def _find_port_owner_pids(self, port: int) -> list[int]:
        """查找占用指定 TCP 端口（LISTENING）的进程 PID 列表。

        Windows 用 netstat -ano，Unix 用 lsof -ti。
        返回去重后的 PID 列表（不含当前进程自身）。
        """
        pids: set[int] = set()
        my_pid = os.getpid()
        try:
            if os.name == "nt":
                # netstat -ano -p TCP 输出形如：
                #   TCP    0.0.0.0:3080      0.0.0.0:0    LISTENING    12345
                #   TCP    [::]:3080         [::]:0      LISTENING     12345
                r = subprocess.run(
                    ["netstat", "-ano", "-p", "TCP"],
                    capture_output=True, text=True, timeout=5, shell=False,
                )
                if r.returncode == 0:
                    for line in r.stdout.splitlines():
                        parts = line.split()
                        if len(parts) < 5:
                            continue
                        if "LISTENING" not in parts:
                            continue
                        # 本地地址列形如 0.0.0.0:3080 或 [::]:3080
                        local = parts[1]
                        if not local.endswith(f":{port}"):
                            continue
                        try:
                            pid = int(parts[-1])
                        except ValueError:
                            continue
                        if pid and pid != my_pid:
                            pids.add(pid)
            else:
                # lsof -ti tcp:PORT 仅返回 PID（每行一个）
                r = subprocess.run(
                    ["lsof", "-ti", f"tcp:{port}"],
                    capture_output=True, text=True, timeout=5, shell=False,
                )
                if r.returncode == 0:
                    for line in r.stdout.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            pid = int(line)
                        except ValueError:
                            continue
                        if pid and pid != my_pid:
                            pids.add(pid)
        except Exception as e:
            log.debug("查找端口 %d 占用进程失败: %s", port, e)
        return list(pids)

    def _kill_port_owner(self, port: int, *, skip_pids: list[int] | None = None) -> int:
        """强制终止占用指定 TCP 端口的所有进程（兜底清理孤儿）。

        用于 stop_dsh 之后清理 taskkill /T 漏杀的脱离进程组子进程
        （shell=True 启动 npx.cmd → node 时偶发）。
        返回被终止的进程数。
        """
        skip = set(skip_pids or [])
        pids = [p for p in self._find_port_owner_pids(port) if p not in skip]
        if not pids:
            return 0
        killed = 0
        for pid in pids:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True, timeout=5, shell=False,
                    )
                else:
                    os.kill(pid, signal.SIGKILL)
                killed += 1
                log.info("已终止占用端口 %d 的孤儿进程 PID=%d", port, pid)
            except Exception as e:
                log.debug("终止端口 %d 占用进程 PID=%d 失败: %s", port, pid, e)
        return killed

    def _stream_logs(self) -> None:
        """透传 DSH 子进程 stdout/stderr 到日志回调。

        额外作用：解析 DSH 启动后打印的 "dsh web: http://host:port" 行，
        把实际监听端口写入 _dsh_ready_port，供 _wait_for_port 立即返回成功
        （比写死 3080 的 socket 探测更快，也能应对 DSH 选了其它端口的情况）。
        """
        if not self._dsh_process or not self._dsh_process.stdout:
            return
        import re

        # 匹配 "dsh web: http://127.0.0.1:3080" / "dsh web: http://localhost:5173" 等
        url_pattern = re.compile(
            r"dsh\s+web\s*:\s*https?://([^/\s:]+):(\d+)", re.IGNORECASE
        )
        try:
            for line in self._dsh_process.stdout:
                if not line:
                    break
                line = line.rstrip()
                if not line:
                    continue
                self._emit_log(line)
                # 记录就绪信号与实际端口
                if not getattr(self, "_dsh_ready_port", None):
                    m = url_pattern.search(line)
                    if m:
                        try:
                            self._dsh_ready_port = int(m.group(2))
                            # 同步更新 PID 文件锁中的端口
                            try:
                                self.pid_lock.acquire(
                                    pid=self._dsh_process.pid,
                                    port=self._dsh_ready_port,
                                )
                            except Exception:
                                pass
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            # 子进程被 terminate 时读 stdout 可能会抛异常，属于正常情况
            log.debug("日志透传结束: %s", e)

    def _wait_for_port(self, timeout: int) -> bool:
        """等待 DSH 在端口上监听。

        两种就绪信号任一触发即成功：
        A. 日志流中已解析到 "dsh web: http://host:port" → 直接对该端口做 socket 探测
        B. 对默认 3080 端口做 socket 探测（兼容旧版本 / 信号解析失败的兜底）

        注意：shell=True 启动 .CMD 时，cmd.exe 包装进程可能在 node.exe 启动后
        就退出（proc.poll() != None），但这不代表 DSH 真正退出了。
        所以只用端口探测做就绪判断，不用 proc.poll()。
        """
        import socket

        deadline = time.time() + timeout
        while time.time() < deadline:
            # A：如果日志里解析到了实际端口，优先对实际端口做探测（最快命中）
            actual_port = getattr(self, "_dsh_ready_port", None)
            probed_ports = []
            if actual_port:
                probed_ports.append((C.DSH_DEFAULT_HOST, actual_port))
            probed_ports.append((C.DSH_DEFAULT_HOST, C.DSH_DEFAULT_PORT))
            # 去重保序
            seen = set()
            dedup_ports: list[tuple[str, int]] = []
            for p in probed_ports:
                if p not in seen:
                    seen.add(p)
                    dedup_ports.append(p)

            for (host, port) in dedup_ports:
                try:
                    with socket.create_connection((host, port), timeout=1):
                        self._emit_log(f"DSH 已就绪 (http://{host}:{port})")
                        return True
                except OSError:
                    pass

            # 仅在 shell=False 时检查子进程退出（shell=True 的 cmd.exe 包装器会提前退出）
            if not getattr(self, "_dsh_use_shell", True):
                if self._dsh_process and self._dsh_process.poll() is not None:
                    self._emit_log("DSH 子进程已退出")
                    return False

            time.sleep(0.5)
        self._emit_log(f"DSH 启动超时（{timeout}s）")
        return False

    def stop_dsh(self) -> bool:
        """停止 DSH 子进程（仅当拥有所有权时）。

        顺序：
          1. terminate/kill 子进程（关闭管道写端）——Windows 下用 taskkill /T 杀进程树
          2. 等待 stdout 透传线程退出（避免 readline 阻塞在已关闭的 PIPE 上仍在跑）
          3. 释放 PID 文件锁
        """
        if not self.is_dsh_owned:
            log.info("无 DSH 进程所有权，不终止（用户手动启动的 DSH 保留运行）")
            return False

        log_thread = getattr(self, "_log_thread", None)

        # 已启动子进程的实际监听端口（dsh web 可能不占用默认 3080）
        port_to_clean = getattr(self, "_dsh_ready_port", None) or C.DSH_DEFAULT_PORT
        killed_pid = getattr(self._dsh_process, "pid", None) if self._dsh_process else None

        if self._dsh_process:
            try:
                # 关键：Windows 下 shell=True 启动的 npx→node 必须用 taskkill /T 杀整棵树
                self._kill_process_tree(self._dsh_process)
                self._emit_log("DSH 子进程已终止")
            except Exception as e:
                log.error("终止 DSH 子进程失败: %s", e)
                return False
            finally:
                self._dsh_process = None

        # 兜底：清理 taskkill /T 漏杀的脱离进程组子进程
        # （shell=True 启动 npx.cmd → node 时，node 偶尔脱离父进程组，
        #  导致 taskkill /T 杀不到，端口 3080 仍被占 → 下次启动触发 PORT_CONFLICT）
        try:
            orphans = self._kill_port_owner(port_to_clean, skip_pids=[killed_pid] if killed_pid else None)
            if orphans:
                self._emit_log(f"已清理 {orphans} 个占用端口 {port_to_clean} 的孤儿进程")
                log.info("stop_dsh 兜底清理孤儿进程: %d 个（端口 %d）", orphans, port_to_clean)
        except Exception as e:
            log.warning("兜底清理端口孤儿进程失败(不致命): %s", e)

        # 子进程终止后 stdout 会被关闭，日志线程通常会很快退出；这里给 2s 等待
        if log_thread is not None and log_thread.is_alive():
            try:
                log_thread.join(timeout=2)
                if log_thread.is_alive():
                    log.warning("DSH 日志透传线程仍未退出（2s 超时），已 daemon 化不阻塞")
            except Exception as e:
                log.warning("等待日志线程退出失败: %s", e)
            finally:
                self._log_thread = None

        self.pid_lock.release()
        self._ownership = ProcessOwnership.NOT_RUNNING
        return True

    def is_alive(self) -> bool:
        """DSH 进程是否存活。"""
        if self._dsh_process and self._dsh_process.poll() is None:
            return True
        return self.pid_lock.is_dsh_alive()
