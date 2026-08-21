"""自动更新检查（P3-4，P1增强版对齐 dsh_desktop client-updater.js）。

启动后在后台线程拉取最新版本号，与当前 APP_VERSION 比较；有新版则通过信号
回到主线程弹窗提示（含下载链接），用户可"前往下载"或"暂不更新"（记录跳过版本）。

设计原则（保留首版全部约束，再加增强）：
  · 非阻塞：网络/解析失败一律静默返回 None，绝不影响启动或主功能。
  · 非强制升级：检查+提示，默认不自动下载替换（桌面应用自动升级风险高）。
  · 可关闭：UserConfig.check_updates 控制是否启用。

P1 新增（对齐 client-updater.js）：
  1. 双源拉取：GitHub Releases API → 失败降级 Gitee Releases API → 自定义 JSON
     环境变量 UPDATE_CHECK_API 可指向自定义镜像 API（与 JS 版 DSH_DESKTOP_RELEASE_API 等价）
  2. 分片 asset 识别：Gitee 单文件 100MB 限制，安装包拆成 .part1/.part2，
     UpdateInfo.assets 里会标注哪些是分片、哪个是便携版/安装版。
  3. 原地替换启动脚本生成器：write_apply_update_bat() 写纯 ASCII .cmd，
     以 detached 方式启动 → 等主进程退出 → 备份旧 exe → 新 exe 替换 → 重启。
     全程写日志到 updates/apply-update.log，用 System32 完整路径避免 PATH 精简。

远端格式：
1. GitHub Releases API（GET .../releases/latest）：
   {"tag_name":"v0.2.0","html_url":"https://...","body":"release notes",
    "assets":[{"name":"DSH-Desktop-0.2.0-portable-x64.exe","browser_download_url":"..."}]}
2. Gitee Releases API（GET .../releases/latest）：assets 结构与 GitHub 一致。
3. 自定义 JSON：{"version":"0.2.0","download_url":"https://...","release_notes":"...",
                 "assets":[{"name":"...","url":"..."}]}
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import constants as C
from ..utils.logger import get_logger

log = get_logger("core.update_checker")


# ============================================================================
# 下载资产信息（新增：用于分片识别）
# ============================================================================
@dataclass
class UpdateAsset:
    """一个下载资产（完整安装包或分片）。"""

    name: str                 # 文件名（如 DSH-Desktop-0.2.0-portable-x64.exe.part1）
    url: str                  # 下载 URL
    size: int = 0             # 字节数（远端有则填）

    @property
    def is_part(self) -> bool:
        return bool(re.search(r"\.part(\d+)$", self.name, re.IGNORECASE))

    @property
    def part_index(self) -> int:
        m = re.search(r"\.part(\d+)$", self.name, re.IGNORECASE)
        return int(m.group(1)) if m else 0

    @property
    def is_portable(self) -> bool:
        return "portable" in self.name.lower()

    @property
    def is_setup(self) -> bool:
        return "setup" in self.name.lower() or self.name.lower().endswith(".exe") and not self.is_portable

    @property
    def base_name(self) -> str:
        """分片的合并后文件名（去掉 .partN）；非分片直接返回 name。"""
        if self.is_part:
            return re.sub(r"\.part\d+$", "", self.name, flags=re.IGNORECASE)
        return self.name


@dataclass
class UpdateInfo:
    """可用更新信息（增强版）。"""

    latest_version: str                  # 远端最新版本号（已去除前导 v）
    current_version: str                 # 当前版本号
    download_url: str                    # 下载页/直链（首版兼容字段）
    release_notes: str = ""              # 更新说明（可为空）
    # P1 新增
    assets: list[UpdateAsset] = field(default_factory=list)  # 所有下载资产（含分片）
    source: str = "custom"               # "github" / "gitee" / "custom"

    def pick_recommended(self, *, portable: bool = True) -> list[UpdateAsset]:
        """为当前部署形态选择推荐的 asset 列表（分片按序号排序后返回）。

        - portable=True  → 选择便携版（*.part1/part2 或 单文件）
        - portable=False → 选择安装版（Setup-*.exe）
        - 找不到对应形态则回退为全部资产
        """
        if not self.assets:
            return []
        # 先过滤目标形态
        target = [a for a in self.assets if (a.is_portable if portable else a.is_setup)]
        if not target:
            target = list(self.assets)
        # 如果有分片：同一个 base_name 的所有分片按序号排序打包返回
        parts_grouped: dict[str, list[UpdateAsset]] = {}
        singles: list[UpdateAsset] = []
        for a in target:
            if a.is_part:
                parts_grouped.setdefault(a.base_name, []).append(a)
            else:
                singles.append(a)
        # 选第一个有分片的组（按 part index 排序），否则选第一个单文件
        for base, parts in parts_grouped.items():
            if parts:
                parts.sort(key=lambda x: x.part_index)
                return parts
        return singles[:1]


# ===== 版本号解析与比较（semver 简化版，支持 pre-release，保持首版 API 不变）=====

_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].+)?")


def _parse(version: str) -> tuple[int, int, int, str] | None:
    """解析版本号为 (major, minor, patch, pre_release)；pre_release 为空串表示正式版。"""
    if not version:
        return None
    version = version.strip().lstrip("vV")
    m = _VERSION_RE.match(version)
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    # 提取 pre-release（-rc.5 中的 rc.5）；无则为空串（正式版）
    rest = version[m.end(3):]
    pre = ""
    if rest.startswith("-"):
        pre = rest[1:].split("+", 1)[0]
    return (major, minor, patch, pre)


def is_newer(latest: str, current: str) -> bool:
    """latest 是否严格新于 current。"""
    a, b = _parse(latest), _parse(current)
    if not a or not b:
        return False
    # 先比主版本号三元组
    if a[:3] != b[:3]:
        return a[:3] > b[:3]
    # 三元组相同：有 pre-release 的版本低于无 pre-release 的正式版
    a_pre, b_pre = a[3], b[3]
    if not a_pre and b_pre:
        return True   # latest 正式版 > current 预发布版
    if a_pre and not b_pre:
        return False  # latest 预发布版 < current 正式版
    if a_pre and b_pre:
        return a_pre > b_pre  # 都有 pre-release，字符串比较（rc.5 > rc.4）
    return False  # 完全相同


# ============================================================================
# 远端拉取：GitHub → Gitee → 自定义 JSON（可被 UPDATE_CHECK_API 环境变量覆盖）
# ============================================================================


def _default_repos() -> dict[str, str]:
    """默认双源仓库（可后续在 constants.py 配置化）。"""
    return {
        # "github": "myYangyunfan/dsh_desktop",
        # "gitee":  "my-yang-yunfan/dsh_desktop",
    }


def _fetch_url_json(url: str, timeout: int) -> dict | None:
    """拉取 JSON，失败静默返回 None。"""
    if not url:
        return None
    import requests

    headers = {"Accept": "application/json"}
    if "api.github.com" in url:
        headers["User-Agent"] = "DSH-Work-Updater"
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.debug("更新检查拉取失败 %s: %s", url, e)
        return None


def _fetch_remote(timeout: int) -> tuple[dict, str] | None:
    """拉取远端并返回 (data_dict, source_name)。

    优先级：
      1. 环境变量 UPDATE_CHECK_API（与 JS 版 DSH_DESKTOP_RELEASE_API 等价）—— 自定义镜像
      2. constants.UPDATE_CHECK_URL —— 兼容首版自定义 JSON
      3. 依次尝试 default_repos 中的 GitHub / Gitee
    """
    # 1. 环境变量覆盖（最高优先级）
    override = os.environ.get("UPDATE_CHECK_API") or os.environ.get("DSH_DESKTOP_RELEASE_API")
    if override:
        data = _fetch_url_json(override, timeout)
        if data:
            return data, "custom-env"

    # 2. 兼容首版：constants.UPDATE_CHECK_URL
    if C.UPDATE_CHECK_URL:
        data = _fetch_url_json(C.UPDATE_CHECK_URL, timeout)
        if data:
            src = "github" if "api.github.com" in C.UPDATE_CHECK_URL else "custom"
            return data, src

    # 3. 默认双源仓库（仅当 constants 中配置了 REPO 时启用，避免硬编码别人仓库）
    repos = getattr(C, "UPDATE_REPOS", None) or _default_repos()
    for src, repo in repos.items():
        if src.lower() == "github":
            url = f"https://api.github.com/repos/{repo}/releases/latest"
        elif src.lower() == "gitee":
            url = f"https://gitee.com/api/v5/repos/{repo}/releases/latest"
        else:
            continue
        data = _fetch_url_json(url, timeout)
        if data:
            return data, src
    return None


def _parse_assets(data: dict, source: str) -> list[UpdateAsset]:
    """从远端 JSON 解析 assets 列表（支持 GitHub/Gitee API 与自定义 JSON）。"""
    assets_raw = None
    # GitHub / Gitee 标准字段
    if isinstance(data.get("assets"), list):
        assets_raw = data["assets"]
    # 自定义 JSON 备用字段
    if assets_raw is None and isinstance(data.get("download_assets"), list):
        assets_raw = data["download_assets"]
    if not assets_raw:
        return []
    out: list[UpdateAsset] = []
    for a in assets_raw:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()
        # GitHub/Gitee 用 browser_download_url；自定义用 url
        url = (
            str(a.get("browser_download_url") or "")
            or str(a.get("url") or "")
            or str(a.get("download_url") or "")
        ).strip()
        if not name or not url:
            continue
        try:
            size = int(a.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append(UpdateAsset(name=name, url=url, size=size))
    return out


def _parse_remote(data: dict, source: str) -> UpdateInfo | None:
    """解析远端 JSON → UpdateInfo；无法识别返回 None。"""
    if not isinstance(data, dict):
        return None

    version = ""
    url = ""
    notes = ""
    # GitHub/Gitee Releases API 格式
    if "tag_name" in data:
        version = str(data.get("tag_name", "")).lstrip("vV")
        url = str(data.get("html_url", "") or data.get("url", "") or "")
        notes = str(data.get("body", "") or "")
    # 自定义 JSON 格式
    elif "version" in data:
        version = str(data.get("version", "")).lstrip("vV")
        url = str(data.get("download_url", "") or data.get("url", "") or "")
        notes = str(data.get("release_notes", "") or data.get("notes", "") or "")

    if not version:
        return None

    assets = _parse_assets(data, source)
    return UpdateInfo(
        latest_version=version,
        current_version=C.APP_VERSION,
        download_url=url,
        release_notes=notes,
        assets=assets,
        source=source,
    )


def check_for_updates(timeout: int = C.UPDATE_CHECK_TIMEOUT_SEC) -> UpdateInfo | None:
    """检查是否有新版本，有则返回 UpdateInfo，否则（含失败）返回 None。

    非阻塞安全：所有异常被捕获，绝不抛出。
    """
    try:
        fetched = _fetch_remote(timeout)
        if not fetched:
            return None
        data, source = fetched
        info = _parse_remote(data, source)
        if not info:
            log.debug("远端更新信息格式无法识别 (source=%s)", source)
            return None
        if not is_newer(info.latest_version, C.APP_VERSION):
            log.debug("已是最新版本: %s (source=%s)", info.latest_version, source)
            return None
        log.info(
            "发现新版本: %s (当前 %s, source=%s, assets=%d)",
            info.latest_version, C.APP_VERSION, source, len(info.assets),
        )
        return info
    except Exception as e:
        log.debug("更新检查异常: %s", e)
        return None


# ============================================================================
# 原地替换升级脚本生成器（对齐 client-updater.js applyUpdate → apply-update.cmd）
# ============================================================================
_MIN_VALID_BYTES = 64 * 1024 * 1024  # 安装包远大于 64MB，避免把错误页 HTML 当 exe


def get_updates_dir() -> Path:
    """下载包 + 脚本存放目录：~/.dsh-work/updates/（与 userData/updates 等价）。"""
    from ..config import get_app_data_dir
    d = get_app_data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _system32_cmd(*, win: bool) -> str:
    """cmd.exe / tasklist / ... 的 System32 绝对路径，避免精简 PATH 找不到。"""
    if not win:
        return "cmd"
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(sysroot, "System32", "cmd.exe")


def write_apply_update_bat(
    *,
    new_exe: str | os.PathLike,
    log_path: str | os.PathLike | None = None,
    portable: bool = True,
) -> Path | None:
    """生成 apply-update.cmd（纯 ASCII，无中文，防代码页问题），返回其路径。

    流程（与 client-updater.js 一致）：
      1. 等 DSH Work 进程退出（tasklist 循环检测，带超时）
      2. 便携版：备份旧 exe → 用新 exe 原地替换 → 重启
         若旧目录只读则退化为直接启动新 exe（保留旧文件）
      3. 安装版：启动新 Setup.exe（安装器自己处理原安装目录覆盖 + 自动重启）

    Args:
        new_exe:  下载完成的新版 exe 路径（便携版：单文件或合并后单文件；安装版：Setup.exe）
        log_path: apply-update.log 路径；默认 updates/apply-update.log
        portable: 是否便携版形态

    Returns:
        .cmd 文件路径；失败返回 None（调用方退化为告知用户手动打开下载目录）
    """
    new_exe_p = Path(os.fspath(new_exe)).resolve()
    if not new_exe_p.is_file():
        log.error("write_apply_update_bat: 新 exe 不存在 %s", new_exe_p)
        return None
    try:
        size = new_exe_p.stat().st_size
        if size < _MIN_VALID_BYTES:
            log.error("write_apply_update_bat: 新 exe 太小 %d bytes，疑似损坏", size)
            return None
    except OSError as e:
        log.error("write_apply_update_bat: 无法 stat 新 exe: %s", e)
        return None

    updates_dir = get_updates_dir()
    log_p = Path(os.fspath(log_path)) if log_path else updates_dir / "apply-update.log"
    bat_path = updates_dir / "apply-update.cmd"
    cur_exe = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve()

    SYS32 = "%SystemRoot%\\System32"
    # 纯 ASCII 脚本，全程写日志
    lines: list[str] = [
        "@echo off",
        "chcp 65001 >nul 2>&1",
        f"set LOG={str(log_p)}",
        f"set NEW={str(new_exe_p)}",
        f"set OLD={str(cur_exe)}",
        "echo [%date% %time%] apply-update start > \"%LOG%\"",
        "",
        "echo [%date% %time%] waiting for DSH Work to exit... >> \"%LOG%\"",
        # 等主进程退出：tasklist | find → pid 不存在即认为退出；超时 ~ 40 秒
        "set WAIT_ROUNDS=40",
        ":wait_loop",
        f"  {SYS32}\\tasklist.exe /FI \"IMAGENAME eq {cur_exe.name}\" /NH 2>nul | {SYS32}\\find.exe /I \"{cur_exe.name}\" >nul 2>nul",
        "  if errorlevel 1 goto wait_done",
        f"  {SYS32}\\ping.exe -n 2 127.0.0.1 >nul",
        "  set /a WAIT_ROUNDS-=1",
        "  if %WAIT_ROUNDS% LEQ 0 goto wait_done",
        "  goto wait_loop",
        ":wait_done",
        "echo [%date% %time%] proceed >> \"%LOG%\"",
        "",
    ]
    if portable:
        backup = str(cur_exe.with_name(cur_exe.stem + ".old" + cur_exe.suffix))
        lines += [
            "rem ===== portable: backup -> replace -> restart =====",
            f"set BAK=\"{backup}\"",
            "if exist %OLD% (",
            "  echo backup %OLD% to %BAK% >> \"%LOG%\"",
            "  copy /Y %OLD% %BAK% >> \"%LOG%\" 2>&1",
            ")",
            "echo replace %OLD% with %NEW% >> \"%LOG%\"",
            "copy /Y %NEW% %OLD% >> \"%LOG%\" 2>&1",
            "if errorlevel 1 (",
            "  echo replace-failed: fallback direct launch >> \"%LOG%\"",
            "  start \"\" %NEW%",
            "  goto end",
            ")",
            "echo restart %OLD% >> \"%LOG%\"",
            "start \"\" %OLD%",
            "goto end",
            "",
        ]
    else:
        lines += [
            "rem ===== setup: launch installer ===== ",
            "echo launch setup: %NEW% >> \"%LOG%\"",
            "start \"\" %NEW%",
            "goto end",
            "",
        ]
    lines += [
        ":end",
        "echo [%date% %time%] apply-update done >> \"%LOG%\"",
        "exit /b 0",
        "",
    ]

    try:
        with open(bat_path, "w", encoding="ascii", errors="ignore", newline="\r\n") as f:
            f.write("\r\n".join(lines))
        log.info("已生成原地替换脚本: %s", bat_path)
        return bat_path
    except OSError as e:
        log.error("写 apply-update.cmd 失败: %s", e)
        return None


def merge_split_parts(parts: list[str | os.PathLike], out_path: str | os.PathLike) -> bool:
    r"""分片合并（Gitee 100MB 限制拆分 .part1/part2），与 copy /b 语义一致。

    优先直接调 System32\cmd.exe /C copy /b（原子、快速、与用户手动合并一致）；
    失败时回退为 Python 二进制拼接（不依赖 shell）。
    """
    if len(parts) < 2:
        log.warning("merge_split_parts: 分片数量不足 %d", len(parts))
        return False
    # 先校验所有分片都存在
    part_paths: list[Path] = []
    total = 0
    for p in parts:
        pp = Path(os.fspath(p)).resolve()
        if not pp.is_file():
            log.error("merge_split_parts: 分片不存在 %s", pp)
            return False
        try:
            total += pp.stat().st_size
        except OSError as e:
            log.error("merge_split_parts: 无法 stat %s: %s", pp, e)
            return False
        part_paths.append(pp)
    if total < _MIN_VALID_BYTES:
        log.error("merge_split_parts: 合并后 %d bytes 太小，疑似异常", total)
        return False
    out_p = Path(os.fspath(out_path)).resolve()
    out_p.parent.mkdir(parents=True, exist_ok=True)
    win = os.name == "nt"
    try:
        if win:
            import subprocess
            parts_str = " + ".join('"' + str(p) + '"' for p in part_paths)
            cmd = "/C copy /b " + parts_str + " \"" + str(out_p) + "\""
            proc = subprocess.run(
                [_system32_cmd(win=True), cmd],
                capture_output=True, text=True, timeout=120, shell=False,
            )
            if proc.returncode == 0 and out_p.is_file() and out_p.stat().st_size >= _MIN_VALID_BYTES:
                log.info("分片合并成功（copy /b）: %s (%d bytes)", out_p, out_p.stat().st_size)
                return True
            log.warning("copy /b 合并失败(rc=%s), 回退 Python 拼接: %s", proc.returncode, proc.stderr[-500:])
    except Exception as e:
        log.warning("copy /b 合并异常, 回退 Python 拼接: %s", e)
    # Python 二进制拼接兜底
    try:
        with open(out_p, "wb") as dst:
            for p in part_paths:
                with open(p, "rb") as src:
                    while True:
                        chunk = src.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
        ok = out_p.is_file() and out_p.stat().st_size >= _MIN_VALID_BYTES
        if ok:
            log.info("分片合并成功（Python 拼接）: %s (%d bytes)", out_p, out_p.stat().st_size)
        return ok
    except OSError as e:
        log.error("分片合并异常: %s", e)
        return False
