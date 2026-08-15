"""安全路径围栏（移植自 dsh_desktop main.js H2/H3 路径围栏）。

目标：防止 DSH Agent 越权写入/打开会话 cwd 之外的敏感路径（如 Startup\\*.bat、
注册表 .reg 脚本、可执行文件等），与 Electron 版 main.js 的 DANGEROUS_EXT +
fileRoots() 设计逐字对齐。

核心语义：
  · is_dangerous_ext(path)          —— 扩展名黑名单拦截
  · is_within_roots(path, roots)    —— 必须位于允许的根目录（cwd / workspace）之下
  · can_open_or_restore(path, roots)—— 两者的组合（实际调用入口）

允许的根目录 roots 由调用方传入（通常是当前会话的 cwd，或当前工作区目录）。
这与 Electron 版的 fileRoots() 语义一致——不假设全局允许列表，由使用场景
（文件预览、文件还原、打开外部编辑器）传入自己的允许根。

缓存：调用方如果需要，自行包一层；这里保持纯函数，便于单元测试。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from ..utils.logger import get_logger

log = get_logger("core.safety_guard")

# ============================================================================
# 危险扩展名黑名单（与 Electron 版 DANGEROUS_EXT 完全对齐，大小写不敏感）
# ============================================================================
# Electron 原版: /\.(bat|cmd|com|exe|ps1|vbs|lnk|js|jse|msi|scr|pif|reg)$/i
# 额外追加：.jar / .wsf / .hta / .inf —— 同样是高风险可执行脚本/安装包
DANGEROUS_EXT_PATTERN = re.compile(
    r"\.(bat|cmd|com|exe|ps1|vbs|lnk|js|jse|msi|scr|pif|reg|jar|wsf|hta|inf)$",
    re.IGNORECASE,
)

# 人类可读描述（用于状态条提示）
DANGEROUS_EXT_DESC = (
    "可执行/脚本文件 (.bat .exe .ps1 .vbs .js .lnk .reg …) 为安全起见已禁止直接预览"
)


def is_dangerous_ext(path: str | os.PathLike) -> bool:
    """判断文件扩展名是否命中黑名单（大小写不敏感）。"""
    if not path:
        return False
    try:
        name = os.path.basename(os.fspath(path))
    except (TypeError, ValueError):
        return False
    if not name:
        return False
    return bool(DANGEROUS_EXT_PATTERN.search(name))


# ============================================================================
# 允许根目录校验（防止绝对路径跳出会话 cwd）
# ============================================================================
def _resolve_abs(path: str | os.PathLike) -> Optional[str]:
    """把路径转成绝对的 realpath；失败返回 None（不抛，避免误拦截）。"""
    try:
        p = Path(os.fspath(path)).expanduser().resolve()
        return str(p)
    except (OSError, RuntimeError, ValueError):
        return None


def is_within_roots(path: str | os.PathLike, roots: Iterable[str | os.PathLike]) -> bool:
    """判断 path 是否位于任一 roots 目录之下（真实路径比较，防符号链接逃逸）。

    与 Electron 版的「文件还原/打开只允许会话 cwd 之下的项目文件」完全一致：
      · 先取 realpath（Windows 下 / Unix 下都能解析符号链接与大小写）
      · roots 中的任意一个如果是 path 的前缀目录 → 通过
      · 如果路径解析失败，保守策略：拒绝
    """
    if not path or not roots:
        return False
    abs_path = _resolve_abs(path)
    if not abs_path:
        return False
    # 末尾加 os.sep，避免 "C:\\work" 命中 "C:\\workspace\\file.py" 的前缀碰撞
    abs_path_norm = abs_path.rstrip(os.sep) + os.sep
    for r in roots:
        if not r:
            continue
        abs_root = _resolve_abs(r)
        if not abs_root:
            continue
        abs_root_norm = abs_root.rstrip(os.sep) + os.sep
        if abs_path_norm.startswith(abs_root_norm):
            return True
        # 边界情况：path 就是 root 本身（例如打开工作区目录的 README）
        if abs_path.rstrip(os.sep) == abs_root.rstrip(os.sep):
            return True
    return False


# ============================================================================
# 组合校验（实际入口）
# ============================================================================
@dataclass  # type: ignore[misc]
class SafetyVerdict:
    """路径安全检查结论。"""

    allowed: bool
    reason: str = ""  # 当 allowed=False 时说明原因

    @property
    def blocked_by_ext(self) -> bool:
        return self.reason == "ext"

    @property
    def blocked_by_roots(self) -> bool:
        return self.reason == "roots"


def can_open_or_restore(
    path: str | os.PathLike,
    roots: Iterable[str | os.PathLike],
) -> SafetyVerdict:
    """组合校验：扩展名 + 白名单根目录。

    Returns:
        SafetyVerdict(allowed, reason)
        reason ∈ { "", "ext", "roots" }
    """
    if is_dangerous_ext(path):
        log.warning("安全围栏：拒绝打开危险扩展名文件 %s", path)
        return SafetyVerdict(allowed=False, reason="ext")
    if roots and not is_within_roots(path, roots):
        log.warning(
            "安全围栏：拒绝打开超出允许根目录的文件 path=%s roots=%s",
            path, [str(r) for r in roots],
        )
        return SafetyVerdict(allowed=False, reason="roots")
    return SafetyVerdict(allowed=True, reason="")


# ============================================================================
# 便捷包装：以当前工作区 + 会话 cwd 为根构造 roots
# ============================================================================
def build_roots_from_context(*, workspace: str | os.PathLike = "", session_cwd: str | os.PathLike = "") -> list[str]:
    """从 UI 常见上下文（工作区 + 会话 cwd）构造允许根目录列表。

    空值自动过滤；返回绝对 realpath 字符串列表，无重复。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (workspace, session_cwd, os.getcwd()):
        if not raw:
            continue
        a = _resolve_abs(raw)
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out


# ============================================================================
# 写保护（AI 写回文件时调用，防止修改会话日志等只读文件）
# ============================================================================

_IMMUTABLE_HINT = (
    # session log zst（由 harness 追加，不应被 AI 覆盖）
    ".session.log.zst", ".session.log",
    # 证书/密钥（AI 通常不该覆盖）
    ".pem", ".key", ".pfx", ".p12",
)


def can_write_workspace(
    path: str | os.PathLike,
    roots: Iterable[str | os.PathLike],
) -> SafetyVerdict:
    """写回文件前的组合校验：扩展名 + 白名单根目录 + 不可变文件拦截。

    比 can_open_or_restore 更严格：
      · 打开是「预览」，写错了只是文件内容坏了；写回是「覆盖/创建」，多一层不可变保护。
      · 不可变：名字以 _IMMUTABLE_HINT 后缀结尾（大小写不敏感）或隐藏的 .session/ 目录。
    """
    # 先复用打开的规则（危险 ext + 根目录）
    open_v = can_open_or_restore(path, roots)
    if not open_v.allowed:
        return open_v
    p_low = str(path).lower().replace("\\", "/")
    for hint in _IMMUTABLE_HINT:
        if p_low.endswith(hint.lower()):
            log.warning("安全围栏：拒绝写入不可变文件 %s (hint=%s)", path, hint)
            return SafetyVerdict(allowed=False, reason="immutable")
    if "/.session/" in p_low or "\\.session\\" in str(path):
        log.warning("安全围栏：拒绝写入 .session 目录 %s", path)
        return SafetyVerdict(allowed=False, reason="immutable")
    return SafetyVerdict(allowed=True, reason="")


# ============================================================================
# Markdown / HTML file:// 链接净化（渲染富文本时用，防止 XSS / 路径穿越）
# ============================================================================


def parse_href_path(href: str, *, base_dir: str | os.PathLike = "") -> Optional[str]:
    """从 href 字符串里解析出本地路径，不合法/非本地返回 None。

    支持：
      · file:///C:/foo/bar.txt  (file URI, 三斜杠=绝对路径)
      · file://localhost/C:/a   (带 host=localhost 的 file URI，等价于本机)
      · ./a.txt  / C:\\a.txt     (相对/绝对原生路径，走 os.path 解析)

    不支持：http/https/ftp/javascript/data/vbscript/... 全部返回 None。
    """
    if not href:
        return None
    s = href.strip()
    if not s:
        return None
    # 黑名单协议前缀（含空行等变形）
    lower = s.lower().lstrip("\ufeff \t\r\n")
    for bad in ("http:", "https:", "ftp:", "javascript:", "data:", "vbscript:", "blob:"):
        if lower.startswith(bad):
            return None
    # 1. file:// 协议
    if lower.startswith("file:"):
        # file://host/path  vs  file:///path
        # 约定：
        #   slashes == 2  → file://host/path  （host 可显式，host=localhost 才接受）
        #   slashes >= 3  → file:///path       （host 为空，绝对路径）
        #   slashes <= 1  → file:relative      （极少见，按相对/原生处理）
        rest = s[5:]
        slashes = 0
        i = 0
        while i < len(rest) and rest[i] == "/":
            slashes += 1
            i += 1
        body = rest[i:]
        host = ""
        path_part = body
        if slashes == 2:
            # 显式 host
            slash_pos = body.find("/")
            if slash_pos >= 0:
                host = body[:slash_pos]
                path_part = body[slash_pos + 1:]
            else:
                host = body
                path_part = ""
            if host and host.lower() != "localhost":
                log.debug("parse_href_path: 非本机 file URI host=%s", host)
                return None
        # Windows 绝对路径 = slashes>=3 时 body 直接是 "C:/foo"
        # Unix 绝对路径 = slashes>=3 时 body 是 "etc/passwd" → 需要补前导 /
        if slashes >= 3:
            if os.name == "nt" and len(body) >= 2 and body[1] == ":":
                raw = body
            else:
                raw = "/" + body if body else ""
        elif slashes == 2 and host == "localhost":
            # file://localhost/C:/foo  → path_part = "C:/foo"
            # file://localhost/etc/passwd → path_part = "etc/passwd" ，补 /
            if os.name == "nt" and len(path_part) >= 2 and path_part[1] == ":":
                raw = path_part
            else:
                raw = "/" + path_part if path_part else ""
        else:
            raw = body
        if not raw:
            return None
        # URL 解码（%20 → 空格等）
        try:
            from urllib.parse import unquote
            raw = unquote(raw)
        except Exception:
            pass
        abs_p = _resolve_abs(raw)
        return abs_p or None

    # 2. 原生路径（相对或绝对）：相对路径会用 base_dir 合成绝对
    try:
        from urllib.parse import unquote
        s_dec = unquote(s)
    except Exception:
        s_dec = s
    if os.path.isabs(s_dec):
        return _resolve_abs(s_dec) or None
    if base_dir:
        joined = os.path.join(os.fspath(base_dir), s_dec)
        return _resolve_abs(joined) or None
    return _resolve_abs(s_dec) or None


def sanitize_file_href(
    href: str,
    *,
    roots: Iterable[str | os.PathLike] = (),
    base_dir: str | os.PathLike = "",
) -> str:
    """净化 Markdown/HTML 中的 file:// 链接。

    Returns:
        - 合法可访问的本地路径：返回形如 "file:///<resolved>" 的标准 file URI
          （注意：Windows 路径会转成 file:///C:/xxx）
        - 其他一切情况（非法/越权/远程协议）：返回 "#"，UI 侧直接显示为不可点击链接
    """
    p = parse_href_path(href, base_dir=base_dir)
    if not p:
        return "#"
    v = can_open_or_restore(p, roots)
    if not v.allowed:
        return "#"
    # 标准 file URI 生成
    try:
        from urllib.parse import quote
        if os.name == "nt":
            # C:\foo → C:/foo → file:///C:/foo
            norm = p.replace("\\", "/")
            if len(norm) >= 2 and norm[1] == ":":
                return "file:///" + quote(norm, safe="/:")
        # Unix → file:///abs/path
        return "file://" + quote("/" + p.lstrip("/"), safe="/")
    except Exception as e:
        log.debug("sanitize_file_href URI 拼装异常: %s", e)
        return "#"

