"""双重自动更新：官方 dsh (npm overlay 原子切换 + 回退) + 客户端自更新（GitHub/Gitee 双源+分片合并）。

整体架构：
  DshUpdater      — 通过 npm registry 获取 dsh 包元信息，与当前版本比较。
                    升级用 `npm install -g dsh@<new>` 执行原子切换；
                    升级后检测 CLI 可用性，失败时用 `npm install -g dsh@<old>` 回退。
  ClientUpdater   — 从 GitHub / Gitee 的 release 接口获取最新 tag、下载 URL、sha256；
                    下载时分 Range 分片（8MB×并发6），落地到 Paths.update_dir()，
                    最后合并并校验 sha256，成功则回调 UI（提示用户重启/走安装）。
  UpdateOrchestrator — 组合两者，统一触发 check + download + apply，回调 UI。
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

from ..paths import Paths
from ..utils.logger import get_logger

log = get_logger("core.updater")

# 分片大小与并发
_CHUNK_SIZE = 8 * 1024 * 1024  # 8MB
_MAX_PARALLEL = 6
# 网络超时
_TIMEOUT = 30
# npm registry
_NPM_REGISTRY_PRIMARY = "https://registry.npmjs.org/dsh/latest"
_NPM_REGISTRY_MIRROR = "https://registry.npmmirror.com/dsh/latest"

# GitHub / Gitee release API（DSH Work 替换成自己的仓库地址；首版使用占位以便用户修改）
# 实际使用时：UserConfig 中应提供"客户端更新仓库"配置，这里取默认公开仓库
_GITHUB_API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"
_GITEE_API_LATEST = "https://gitee.com/api/v5/repos/{repo}/releases/latest"
_DEFAULT_GITHUB_REPO = "zouyuxuan122/Deepseek-Harness-EAC"  # 原作者仓库（用户可在设置改）
_DEFAULT_GITEE_REPO = "zouyuxuan122/Deepseek-Harness-EAC"


# =====================================================================
#  数据结构
# =====================================================================

@dataclass
class DshUpdateInfo:
    current_version: str
    latest_version: str
    has_update: bool
    error: str = ""
    npm_tarball_url: str = ""

    @property
    def need_update(self) -> bool:
        return self.has_update and not self.error


@dataclass
class ClientReleaseInfo:
    version: str
    published_at: str = ""
    html_url: str = ""
    download_url: str = ""
    size: int = 0
    sha256: str = ""
    release_notes: str = ""
    source: str = ""  # "github" / "gitee"
    error: str = ""

    @property
    def is_valid(self) -> bool:
        return bool(self.version and self.download_url and not self.error)


@dataclass
class ApplyResult:
    ok: bool
    message: str
    rollback_used: bool = False
    output_path: str = ""  # 客户端新包落地路径 / dsh 新版信息
    restart_required: bool = False


# =====================================================================
#  工具：SemVer 比较
# =====================================================================

_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+][\w.\-+]*)?$")


def _parse_semver(v: str) -> tuple[int, int, int] | None:
    v = v.strip().lstrip("vV")
    m = _SEMVER_RE.match(v)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _semver_gt(a: str, b: str) -> bool:
    """如果 a > b 返回 True。任何一端 None 返回 False。"""
    pa, pb = _parse_semver(a), _parse_semver(b)
    if pa is None or pb is None:
        return False
    return pa > pb


# =====================================================================
#  官方 dsh 更新器
# =====================================================================

class DshUpdater:
    """检测并升级官方 dsh CLI。"""

    def __init__(self, npm_executable: str | None = None,
                 current_version: str | None = None,
                 registry_mirror: str | None = None):
        self.npm = npm_executable or shutil.which("npm") or "npm"
        self._forced_current = current_version
        self.registry_mirror = registry_mirror

    # ---------------- 查询 ----------------

    def detect_current_version(self) -> str:
        if self._forced_current:
            return self._forced_current
        try:
            cp = subprocess.run([self.npm, "ls", "-g", "--depth=0", "--json", "dsh"],
                                capture_output=True, text=True, timeout=15)
            if cp.returncode == 0 and cp.stdout.strip():
                data = json.loads(cp.stdout)
                v = (data.get("dependencies") or {}).get("dsh", {}).get("version", "")
                if v:
                    return v
        except (OSError, ValueError, subprocess.TimeoutExpired) as e:
            log.debug("读取 dsh 当前版本（npm ls）失败: %s", e)
        # 回退：调用 dsh --version
        try:
            dsh = shutil.which("dsh") or "dsh"
            cp = subprocess.run([dsh, "--version"],
                                capture_output=True, text=True, timeout=10)
            if cp.returncode == 0:
                out = (cp.stdout or cp.stderr or "").strip()
                m = _SEMVER_RE.search(out)
                if m:
                    return ".".join(m.groups()[:3])
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ""

    def check_update(self, timeout: int = _TIMEOUT) -> DshUpdateInfo:
        current = self.detect_current_version()
        url = self.registry_mirror if self.registry_mirror else _NPM_REGISTRY_PRIMARY
        err = ""
        latest = ""
        tarball = ""
        tried: list[str] = [url]
        if url != _NPM_REGISTRY_MIRROR:
            tried.append(_NPM_REGISTRY_MIRROR)
        for u in tried:
            try:
                data = self._http_get(u, timeout=timeout)
                latest = str(data.get("version") or "")
                tarball = str(data.get("dist", {}).get("tarball", "") or "")
                err = ""
                if latest:
                    break
            except Exception as e:
                err = f"查询失败: {e}"
        if not latest:
            return DshUpdateInfo(current_version=current, latest_version="",
                                 has_update=False, error=err or "未获取到最新版本号。")
        has_update = _semver_gt(latest, current)
        return DshUpdateInfo(current_version=current, latest_version=latest,
                             has_update=has_update, error=err, npm_tarball_url=tarball)

    # ---------------- 应用 ----------------

    def apply_update(self, info: DshUpdateInfo,
                     on_progress: Callable[[str], None] | None = None,
                     ) -> ApplyResult:
        if not info.need_update or not info.latest_version:
            return ApplyResult(False, "无可用更新。")
        if on_progress:
            on_progress(f"开始升级官方 dsh: {info.current_version} → {info.latest_version}")
        old_ver = info.current_version
        new_ver = info.latest_version
        # 升级
        rc, out = self._npm_global_install(f"dsh@{new_ver}")
        if rc != 0:
            return ApplyResult(False, f"npm install 失败（{rc}）: {out}")
        # 校验新版本可用
        new_real = self.detect_current_version()
        if new_real != new_ver and not _semver_gt(new_real or "0.0.0", old_ver or "0.0.0"):
            # 回退
            log.warning("新版本 dsh 不可用（检测到 %s），开始回退", new_real)
            rc2, out2 = self._npm_global_install(f"dsh@{old_ver}") if old_ver else (-1, "")
            return ApplyResult(
                False,
                f"升级后校验失败，检测版本为 {new_real!r}。已尝试回退："
                + ("成功" if rc2 == 0 else f"失败（{rc2}） {out2}"),
                rollback_used=(rc2 == 0),
            )
        if on_progress:
            on_progress(f"官方 dsh 升级完成：{new_real}")
        return ApplyResult(True, f"官方 dsh 已升级到 {new_real}", restart_required=False,
                           output_path=f"dsh@{new_real}")

    def _npm_global_install(self, spec: str) -> tuple[int, str]:
        try:
            cp = subprocess.run(
                [self.npm, "install", "-g", spec],
                capture_output=True, text=True, timeout=600,
            )
            return cp.returncode, (cp.stderr or cp.stdout or "")
        except OSError as e:
            return -1, f"执行 npm 失败: {e}"
        except subprocess.TimeoutExpired:
            return -1, "npm 执行超时（>10min）。"

    # ---------------- 通用 ----------------

    def _http_get(self, url: str, timeout: int) -> dict:
        with self._build_session() as s:
            r = s.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()

    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        a = HTTPAdapter(pool_connections=5, pool_maxsize=10)
        s.mount("https://", a)
        s.mount("http://", a)
        return s


# =====================================================================
#  客户端自更新器（GitHub / Gitee 双源）
# =====================================================================

class ClientUpdater:
    def __init__(self,
                 current_version: str,
                 github_repo: str | None = None,
                 gitee_repo: str | None = None,
                 platform_tag: str | None = None):
        self.current_version = current_version
        self.github_repo = github_repo or _DEFAULT_GITHUB_REPO
        self.gitee_repo = gitee_repo or _DEFAULT_GITEE_REPO
        # 当前平台标签，用于匹配 asset 名（Windows-amd64 / macOS-arm64 / linux-x64 ...）
        self.platform_tag = platform_tag or self._default_platform_tag()

    @staticmethod
    def _default_platform_tag() -> str:
        arch = platform.machine().lower()
        arch_map = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}
        arch_norm = arch_map.get(arch, arch)
        sysname = platform.system().lower()  # windows / darwin / linux
        return f"{sysname}-{arch_norm}"

    # ---------------- 检查更新 ----------------

    def check_update(self, sources: tuple[str, ...] = ("github", "gitee"),
                     timeout: int = _TIMEOUT) -> ClientReleaseInfo:
        errors: list[str] = []
        best: ClientReleaseInfo | None = None
        session = DshUpdater._build_session()
        for src in sources:
            try:
                rel = self._fetch_release(session, src, timeout)
            except Exception as e:
                errors.append(f"{src}: {e}")
                continue
            if not rel.is_valid:
                errors.append(f"{src}: release 缺少下载链接")
                continue
            # 选择版本号更高的一个
            if best is None or _semver_gt(rel.version, best.version):
                best = rel
        if best is None:
            return ClientReleaseInfo(version="", error="；".join(errors) or "未找到 release。")
        if not _semver_gt(best.version, self.current_version):
            best.error = f"当前版本已是最新（{self.current_version}）。"
            best.source = best.source  # keep
            return best
        return best

    def _fetch_release(self, session: requests.Session, src: str,
                       timeout: int) -> ClientReleaseInfo:
        if src == "github":
            url = _GITHUB_API_LATEST.format(repo=self.github_repo)
        elif src == "gitee":
            url = _GITEE_API_LATEST.format(repo=self.gitee_repo)
        else:
            raise ValueError(f"未知更新源: {src}")
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        version = str(data.get("tag_name") or data.get("name") or "").lstrip("vV")
        html_url = str(data.get("html_url") or data.get("target_commit_url") or "")
        published_at = str(data.get("published_at") or data.get("created_at") or "")
        notes = str(data.get("body") or "")
        # 找匹配当前平台的资产
        assets = data.get("assets") or []
        chosen = self._pick_asset(assets)
        if chosen is None:
            return ClientReleaseInfo(version=version, html_url=html_url,
                                     published_at=published_at, release_notes=notes,
                                     source=src, error="未找到匹配当前平台的资产。")
        download_url = str(chosen.get("browser_download_url") or chosen.get("download_url") or "")
        size = int(chosen.get("size") or 0)
        # sha256：可选（release 提供名为 sha256.txt 的资产时下载解析）
        sha256 = ""
        sha_asset = next((a for a in assets if _looks_like_sha256_asset(a, chosen)), None)
        if sha_asset is not None:
            try:
                sha256 = self._fetch_sha256(session, sha_asset, timeout, chosen.get("name", ""))
            except Exception as e:
                log.warning("解析 release sha256 失败: %s", e)
        return ClientReleaseInfo(
            version=version,
            published_at=published_at,
            html_url=html_url,
            download_url=download_url,
            size=size,
            sha256=sha256,
            release_notes=notes,
            source=src,
        )

    def _pick_asset(self, assets: list) -> dict | None:
        tag = self.platform_tag.lower()
        sysname = platform.system().lower()  # windows
        # 1) 精确匹配 platform_tag
        for a in assets:
            name = str(a.get("name") or "").lower()
            if tag and tag in name:
                return a
        # 2) 系统名 + .exe / 压缩包扩展名
        ext_pref = [".exe", ".msi", ".zip", ".tar.gz", ".dmg", ".deb", ".rpm", ".AppImage"]
        for ext in ext_pref:
            for a in assets:
                name = str(a.get("name") or "").lower()
                if sysname in name and name.endswith(ext):
                    return a
        # 3) 退而求其次，匹配扩展名
        for ext in ext_pref:
            for a in assets:
                name = str(a.get("name") or "").lower()
                if name.endswith(ext):
                    return a
        return None if not assets else assets[0]

    def _fetch_sha256(self, session, asset, timeout: int, target_name: str) -> str:
        url = asset.get("browser_download_url") or asset.get("download_url")
        if not url:
            return ""
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        text = r.text
        # sha256 校验文件一般是 "<hash>  <filename>"
        for line in text.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                h, name = parts[0], parts[1].lstrip("* ")
                if name == target_name and len(h) == 64:
                    return h.lower()
        if text.strip() and len(text.strip()) == 64:
            return text.strip().lower()
        return ""

    # ---------------- 下载（分片+并发） ----------------

    def download_release(self, release: ClientReleaseInfo,
                         on_progress: Callable[[int, int], None] | None = None,
                         ) -> Path:
        if not release.is_valid:
            raise RuntimeError("release 无效，无法下载。")
        download_dir = Paths.update_dir()
        download_dir.mkdir(parents=True, exist_ok=True)
        file_name = _url_name(release.download_url)
        if not file_name:
            file_name = f"DSHWork-{release.version}-{self.platform_tag}.bin"
        final_path = download_dir / file_name
        # 若已存在且 size/sha256 匹配 → 复用
        if final_path.exists() and release.size and final_path.stat().st_size == release.size:
            if release.sha256 and _file_sha256(final_path) == release.sha256.lower():
                log.info("新安装包已存在，跳过下载: %s", final_path)
                return final_path
        total_size = release.size if release.size else self._probe_size(release.download_url)
        parts_dir = download_dir / f"{file_name}.parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        # 计算分片
        ranges: list[tuple[int, int]] = []
        if total_size and total_size > _CHUNK_SIZE * 2:
            start = 0
            idx = 0
            while start < total_size:
                end = min(total_size, start + _CHUNK_SIZE) - 1
                ranges.append((start, end))
                start = end + 1
                idx += 1
        # 下载分片 or 单线程
        downloaded_bytes = 0
        if ranges:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_PARALLEL) as pool:
                futures = {}
                for i, (a, b) in enumerate(ranges):
                    part_path = parts_dir / f"part_{i:04d}"
                    ft = pool.submit(self._range_download, release.download_url, a, b, part_path)
                    futures[ft] = (i, a, b, part_path)
                for ft in concurrent.futures.as_completed(futures):
                    i, a, b, part_path = futures[ft]
                    n = ft.result()
                    downloaded_bytes += n
                    if on_progress is not None:
                        try:
                            on_progress(downloaded_bytes, total_size)
                        except Exception:
                            pass
            # 合并
            with final_path.open("wb") as out:
                for i in range(len(ranges)):
                    p = parts_dir / f"part_{i:04d}"
                    out.write(p.read_bytes())
                    try:
                        p.unlink()
                    except OSError:
                        pass
            try:
                parts_dir.rmdir()
            except OSError:
                pass
        else:
            # 单线程直下
            self._range_download(release.download_url, None, None, final_path,
                                 on_progress=on_progress)
        # 校验 sha256
        if release.sha256:
            real = _file_sha256(final_path).lower()
            if real != release.sha256.lower():
                try:
                    final_path.unlink()
                except OSError:
                    pass
                raise RuntimeError(f"安装包 SHA256 不匹配：expect={release.sha256}, actual={real}")
        return final_path

    def _range_download(self, url: str, start: int | None, end: int | None,
                        dest: Path,
                        on_progress: Callable[[int, int], None] | None = None) -> int:
        sess = DshUpdater._build_session()
        headers = {"Range": f"bytes={start}-{end}"} if start is not None else {}
        total_hint = (end - start + 1) if start is not None and end is not None else 0
        acc = 0
        with sess.get(url, headers=headers, stream=True, timeout=_TIMEOUT * 3) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    acc += len(chunk)
                    if on_progress is not None and total_hint:
                        try:
                            on_progress(acc, total_hint)
                        except Exception:
                            pass
        return acc

    def _probe_size(self, url: str) -> int:
        try:
            sess = DshUpdater._build_session()
            with sess.head(url, allow_redirects=True, timeout=_TIMEOUT) as r:
                if r.status_code < 400:
                    try:
                        return int(r.headers.get("Content-Length", "0") or 0)
                    except ValueError:
                        return 0
        except Exception as e:
            log.debug("探测 Content-Length 失败: %s", e)
        return 0


def _looks_like_sha256_asset(asset: dict, chosen: dict) -> bool:
    name = str(asset.get("name") or "").lower()
    if not name:
        return False
    target_name = str(chosen.get("name") or "").lower()
    if target_name and target_name[:8] in name:
        pass
    return (name.endswith(".sha256") or name.endswith(".sha256.txt")
            or "sha256" in name or "checksum" in name)


def _url_name(url: str) -> str:
    from urllib.parse import urlparse, unquote
    path = unquote(urlparse(url).path)
    return os.path.basename(path) if path else ""


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# =====================================================================
#  协调器：统一检查 / 应用
# =====================================================================

class UpdateOrchestrator:
    """同时检查 官方 dsh + 客户端 更新，并发下载与应用。"""

    def __init__(self, client_version: str):
        self.dsh = DshUpdater()
        self.client = ClientUpdater(current_version=client_version)

    def check_all(self, parallel: bool = True
                  ) -> tuple[DshUpdateInfo, ClientReleaseInfo]:
        if parallel:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(self.dsh.check_update)
                f2 = pool.submit(self.client.check_update)
                return f1.result(), f2.result()
        return self.dsh.check_update(), self.client.check_update()

    def apply_dsh(self, info: DshUpdateInfo,
                  on_progress: Callable[[str], None] | None = None) -> ApplyResult:
        return self.dsh.apply_update(info, on_progress=on_progress)

    def download_client(self, rel: ClientReleaseInfo,
                        on_progress: Callable[[int, int], None] | None = None) -> Path:
        return self.client.download_release(rel, on_progress=on_progress)

    # 便捷：一键应用"能升级的都升级"
    def apply_all(self, *,
                  apply_dsh: bool = True,
                  download_client: bool = True,
                  on_log: Callable[[str], None] | None = None,
                  on_client_progress: Callable[[int, int], None] | None = None,
                  ) -> tuple[ApplyResult, ApplyResult]:
        dsh_info, client_info = self.check_all()
        dsh_res = ApplyResult(False, "未要求升级官方 dsh。")
        client_res = ApplyResult(False, "未要求下载客户端更新包。")
        if apply_dsh and dsh_info.need_update:
            try:
                dsh_res = self.apply_dsh(dsh_info, on_progress=on_log)
            except Exception as e:
                dsh_res = ApplyResult(False, f"官方 dsh 升级异常: {e}")
        if download_client and client_info.is_valid and _semver_gt(client_info.version,
                                                                  self.client.current_version):
            try:
                p = self.download_client(client_info, on_progress=on_client_progress)
                client_res = ApplyResult(
                    True,
                    f"客户端更新包已下载：{p}\n关闭程序后手动运行安装包即可。",
                    output_path=str(p),
                    restart_required=True,
                )
                if on_log:
                    on_log(client_res.message)
            except Exception as e:
                client_res = ApplyResult(False, f"客户端更新下载失败: {e}")
        return dsh_res, client_res
