"""一键迁移：从 Codex / Claude Code 目录把 skills / MCP / 记忆迁移到 DSH Work。

识别来源：
  - Codex: 常见是一个项目目录结构，含 `skills/` 子目录 + `mcp/` + `config.yaml/.json`
  - Claude Code (Anthropic 官方 CLI): 通常是 `~/.claude/` 下 skills/、settings.json、
    conversations/ / memories/（或 sqlite 数据库）

迁移产物落地到：
  - Paths.user_data() / "skills"   → 每个 skill 一个子目录（同名合并时加时间戳后缀）
  - Paths.user_data() / "mcp"      → MCP 配置 JSON（migrated-<source>-<ts>.json）
  - Paths.migrations_dir()         → 每次迁移的完整报告 JSON（失败原因、跳过列表）
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore

from ..paths import Paths
from ..utils.logger import get_logger

log = get_logger("core.migration")


# =====================================================================
#  数据结构
# =====================================================================

SOURCE_CODEX = "codex"
SOURCE_CLAUDE_CODE = "claude-code"
SOURCE_AUTO = "auto"

_SUPPORTED_SKILL_EXT = {".md", ".txt", ".yaml", ".yml", ".json", ".py", ".sh", ".ps1"}


@dataclass
class MigrationItem:
    category: str       # "skill" / "mcp-config" / "memory"
    source_path: str
    dest_path: str = ""
    status: str = "pending"   # pending / copied / merged / skipped / failed
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "source": self.source_path,
            "dest": self.dest_path,
            "status": self.status,
            "message": self.message,
        }


@dataclass
class MigrationPlan:
    source: str         # "codex" / "claude-code"
    source_root: str
    items: list[MigrationItem] = field(default_factory=list)
    detected_at: float = 0.0

    @property
    def total(self) -> int:
        return len(self.items)

    def summary(self) -> dict:
        by_cat: dict[str, int] = {}
        for it in self.items:
            by_cat[it.category] = by_cat.get(it.category, 0) + 1
        return {"source": self.source, "root": self.source_root,
                "total": self.total, "by_category": by_cat}


@dataclass
class MigrationReport:
    source: str
    source_root: str
    started_at: float
    finished_at: float = 0.0
    items: list[MigrationItem] = field(default_factory=list)
    dest_skills_dir: str = ""
    dest_mcp_file: str = ""
    dest_memory_dir: str = ""
    note: str = ""

    def by_status(self) -> dict[str, int]:
        s: dict[str, int] = {}
        for it in self.items:
            s[it.status] = s.get(it.status, 0) + 1
        return s

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "source": self.source,
                "source_root": self.source_root,
                "started_at": self.started_at,
                "started_at_h": datetime.fromtimestamp(self.started_at).isoformat(),
                "finished_at": self.finished_at,
                "finished_at_h": datetime.fromtimestamp(self.finished_at).isoformat()
                                  if self.finished_at else "",
                "dest_skills_dir": self.dest_skills_dir,
                "dest_mcp_file": self.dest_mcp_file,
                "dest_memory_dir": self.dest_memory_dir,
                "note": self.note,
                "status_summary": self.by_status(),
                "items": [it.to_dict() for it in self.items],
            },
            ensure_ascii=False, indent=indent,
        )


# =====================================================================
#  迁移器
# =====================================================================

class Migrator:
    """Codex / Claude Code → DSH Work 一键迁移。"""

    def __init__(self, source_root: str | Path | None = None,
                 source: str = SOURCE_AUTO):
        self._explicit_root = Path(source_root).expanduser().resolve() if source_root else None
        self._explicit_source = source

    # ---------------- 来源探测 ----------------

    def detect(self) -> MigrationPlan:
        root, src = self._resolve_source()
        if root is None:
            raise RuntimeError(
                "未检测到 Codex 或 Claude Code 目录。\n"
                "请在 设置 → 一键迁移 中手动指定源目录，或确保已安装对应工具。"
            )
        items: list[MigrationItem] = []
        self._scan_skills(root, src, items)
        self._scan_mcp(root, src, items)
        self._scan_memories(root, src, items)
        return MigrationPlan(
            source=src, source_root=str(root), items=items, detected_at=time.time(),
        )

    def _resolve_source(self) -> tuple[Path | None, str]:
        if self._explicit_root is not None:
            # 用户指定：按结构判断
            src = self._classify(self._explicit_root)
            if src != SOURCE_AUTO:
                return self._explicit_root, src
            # 结构无法判断时，用用户提供的 explicit_source
            forced = self._explicit_source if self._explicit_source in {SOURCE_CODEX, SOURCE_CLAUDE_CODE} else None
            if forced:
                return self._explicit_root, forced
            # 最后假定 Codex（项目目录结构最常见）
            return self._explicit_root, SOURCE_CODEX
        # 自动探测
        home = Path.home()
        candidates: list[tuple[Path, str]] = [
            (home / ".claude", SOURCE_CLAUDE_CODE),
            (home / ".codex", SOURCE_CODEX),
            (home / ".config" / "claude", SOURCE_CLAUDE_CODE),
            (home / ".config" / "codex", SOURCE_CODEX),
            # IDE 插件常放的数据目录
            (home / "AppData" / "Roaming" / "Codex", SOURCE_CODEX),
            (home / "Library" / "Application Support" / "Claude", SOURCE_CLAUDE_CODE),
        ]
        for p, _hint in candidates:
            if p.exists():
                cls = self._classify(p)
                if cls != SOURCE_AUTO:
                    return p, cls
        return None, SOURCE_AUTO

    @staticmethod
    def _classify(root: Path) -> str:
        """根据目录结构归类来源。"""
        if not root.exists():
            return SOURCE_AUTO
        names = {p.name.lower() for p in root.iterdir()}
        # Claude Code 典型标志
        claude_marks = {"settings.json", "skills", "conversations", "memories", "claude.log"}
        claude_score = sum(1 for m in claude_marks if m in names)
        # Codex 典型标志
        codex_marks = {"mcp", "skills", "config.yaml", "config.json", "prompts"}
        codex_score = sum(1 for m in codex_marks if m in names)
        if claude_score > codex_score and claude_score >= 1:
            return SOURCE_CLAUDE_CODE
        if codex_score >= 1:
            return SOURCE_CODEX
        return SOURCE_AUTO

    # ---------------- 扫描（生成 plan） ----------------

    def _scan_skills(self, root: Path, source: str, items: list[MigrationItem]) -> None:
        roots_to_try: list[Path] = []
        if source == SOURCE_CLAUDE_CODE:
            roots_to_try.append(root / "skills")
            roots_to_try.append(root / "custom_skills")
        else:  # codex
            roots_to_try.append(root / "skills")
            roots_to_try.append(root / "custom_skills")
            roots_to_try.append(root / "prompts")
        for skills_root in roots_to_try:
            if not skills_root.exists() or not skills_root.is_dir():
                continue
            for entry in skills_root.iterdir():
                try:
                    if entry.is_file() and entry.suffix.lower() in _SUPPORTED_SKILL_EXT:
                        items.append(MigrationItem(category="skill", source_path=str(entry)))
                    elif entry.is_dir():
                        # 子目录技能（含 README.md / SKILL.md / 配置）
                        if any(p.suffix.lower() in _SUPPORTED_SKILL_EXT for p in entry.iterdir()
                               if p.is_file()):
                            items.append(MigrationItem(category="skill", source_path=str(entry)))
                except OSError as e:
                    log.debug("扫描 skill 失败 %s: %s", entry, e)

    def _scan_mcp(self, root: Path, source: str, items: list[MigrationItem]) -> None:
        # Codex: mcp/ 目录 / config.yaml / config.json
        # Claude Code: settings.json 中的 mcpServers
        if source == SOURCE_CODEX:
            cfg = root / "config.yaml"
            if cfg.exists():
                items.append(MigrationItem(category="mcp-config", source_path=str(cfg)))
            cfg = root / "config.json"
            if cfg.exists():
                items.append(MigrationItem(category="mcp-config", source_path=str(cfg)))
            mcp_dir = root / "mcp"
            if mcp_dir.exists() and mcp_dir.is_dir():
                for p in mcp_dir.iterdir():
                    if p.is_file() and p.suffix.lower() in {".yaml", ".yml", ".json"}:
                        items.append(MigrationItem(category="mcp-config", source_path=str(p)))
        else:  # claude-code
            settings = root / "settings.json"
            if settings.exists():
                items.append(MigrationItem(category="mcp-config", source_path=str(settings)))

    def _scan_memories(self, root: Path, source: str, items: list[MigrationItem]) -> None:
        # Codex: 常见 memories/ 目录 / memory.db
        # Claude Code: conversations/ / memories/ / state.db
        mem_dirs = [root / "memories", root / "conversations", root / "memory"]
        for d in mem_dirs:
            if d.exists() and d.is_dir():
                items.append(MigrationItem(category="memory", source_path=str(d)))
        for db_name in ["state.db", "memory.db", "conversations.db"]:
            p = root / db_name
            if p.exists() and p.is_file():
                items.append(MigrationItem(category="memory", source_path=str(p)))

    # ---------------- 执行迁移 ----------------

    def apply(self, plan: MigrationPlan | None = None) -> MigrationReport:
        if plan is None:
            plan = self.detect()
        started = time.time()
        report = MigrationReport(
            source=plan.source,
            source_root=plan.source_root,
            started_at=started,
            items=list(plan.items),
        )
        # 目标目录
        skills_dest = Paths.user_data() / "skills"
        mcp_dest_dir = Paths.user_data() / "mcp"
        mem_dest = Paths.user_data() / "memories"
        for p in [skills_dest, mcp_dest_dir, mem_dest]:
            p.mkdir(parents=True, exist_ok=True)
        report.dest_skills_dir = str(skills_dest)
        report.dest_memory_dir = str(mem_dest)
        # 迁移产物：MCP 配置单独落地为合并 JSON（每个来源一个文件）
        mcp_merged: dict[str, Any] = {"migrated_from": plan.source,
                                      "source_root": plan.source_root,
                                      "migrated_at": started,
                                      "mcpServers": {}}

        for it in report.items:
            try:
                if it.category == "skill":
                    dest = self._copy_skill(Path(it.source_path), skills_dest)
                    it.dest_path = str(dest)
                    it.status = "copied"
                elif it.category == "mcp-config":
                    servers = self._parse_mcp(Path(it.source_path), plan.source)
                    for name, svr in servers.items():
                        if name in mcp_merged["mcpServers"]:
                            # 重名加后缀
                            new_name = f"{name}_{int(time.time()*1000)}"
                            it.message += (f" 重命名 MCP server {name} → {new_name}。")
                            mcp_merged["mcpServers"][new_name] = svr
                        else:
                            mcp_merged["mcpServers"][name] = svr
                    it.status = "merged"
                    it.message = (it.message + f" 提取 MCP server {len(servers)} 项。").strip()
                elif it.category == "memory":
                    dest = self._copy_memory(Path(it.source_path), mem_dest, plan.source)
                    it.dest_path = str(dest)
                    it.status = "copied"
                else:
                    it.status = "skipped"
                    it.message = "未知分类。"
            except Exception as e:
                log.warning("迁移条目失败 %s: %s", it.source_path, e)
                it.status = "failed"
                it.message = str(e)

        # 写 MCP 合并文件
        if mcp_merged["mcpServers"]:
            ts = int(started)
            mcp_out = mcp_dest_dir / f"migrated-{plan.source}-{ts}.json"
            mcp_out.write_text(json.dumps(mcp_merged, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            report.dest_mcp_file = str(mcp_out)

        report.finished_at = time.time()
        # 持久化报告
        try:
            report_path = Paths.migrations_dir() / (
                f"{plan.source}-{int(report.finished_at)}.json"
            )
            report_path.write_text(report.to_json(), encoding="utf-8")
            report.note = f"迁移报告已保存：{report_path}"
        except OSError as e:
            report.note = f"保存迁移报告失败: {e}"
        return report

    # ---------------- 具体迁移实现 ----------------

    def _copy_skill(self, src: Path, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        if dest.exists():
            # 冲突：加时间戳
            stem = src.stem
            suf = src.suffix
            dest = dest_dir / f"{stem}-migrated-{int(time.time()*1000)}{suf}"
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        return dest

    def _parse_mcp(self, src: Path, source: str) -> dict[str, dict]:
        """解析 MCP 配置文件为 {serverName: {command, args, env}} 字典。"""
        servers: dict[str, dict] = {}
        text = src.read_text(encoding="utf-8", errors="replace")
        if src.suffix.lower() in {".yaml", ".yml"}:
            try:
                data = yaml.safe_load(text) or {}
            except Exception as e:
                raise RuntimeError(f"YAML 解析失败: {e}") from e
        else:
            try:
                data = json.loads(text)
            except ValueError as e:
                raise RuntimeError(f"JSON 解析失败: {e}") from e

        if not isinstance(data, dict):
            return servers

        # Claude Code：顶层 mcpServers
        if source == SOURCE_CLAUDE_CODE:
            raw = data.get("mcpServers") or data.get("mcp") or {}
            if isinstance(raw, dict):
                for name, cfg in raw.items():
                    if isinstance(cfg, dict):
                        servers[str(name)] = self._normalize_mcp_cfg(cfg)
        else:
            # Codex：顶层 mcp 或 servers 或直接是 server dict 的 dict
            for key in ["mcpServers", "mcp", "servers", "tools"]:
                raw = data.get(key)
                if isinstance(raw, dict):
                    for name, cfg in raw.items():
                        if isinstance(cfg, dict):
                            servers[str(name)] = self._normalize_mcp_cfg(cfg)
                    if servers:
                        return servers
            # 如果没有明显的外层 key，可能顶层本身就是 servers dict
            for name, cfg in data.items():
                if isinstance(cfg, dict) and ("command" in cfg or "cmd" in cfg or "url" in cfg):
                    servers[str(name)] = self._normalize_mcp_cfg(cfg)
        return servers

    @staticmethod
    def _normalize_mcp_cfg(cfg: dict) -> dict:
        """把各种格式的 MCP server 配置统一为 {command, args, env, url}。"""
        out: dict[str, Any] = {}
        cmd = cfg.get("command") or cfg.get("cmd") or cfg.get("run")
        if cmd:
            out["command"] = str(cmd)
        args = cfg.get("args") or cfg.get("arguments") or cfg.get("argv")
        if isinstance(args, list):
            out["args"] = [str(a) for a in args]
        env = cfg.get("env")
        if isinstance(env, dict):
            out["env"] = {str(k): str(v) for k, v in env.items()}
        url = cfg.get("url") or cfg.get("url_sse") or cfg.get("endpoint")
        if url:
            out["url"] = str(url)
        return out

    def _copy_memory(self, src: Path, dest_dir: Path, source: str) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        dest_name = f"migrated-{source}-{ts}-{src.name}"
        dest = dest_dir / dest_name
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        # 如果是 sqlite，额外做一个只读转储，防止打开被锁
        if src.is_file() and _looks_like_sqlite(src):
            dump_dest = dest_dir / f"{dest_name}.dump.jsonl"
            try:
                self._dump_sqlite(src, dump_dest)
            except Exception as e:
                log.warning("sqlite 转储失败 %s: %s", src, e)
        return dest

    @staticmethod
    def _dump_sqlite(db: Path, dest: Path) -> None:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [r[0] for r in cur.fetchall()]
            with dest.open("w", encoding="utf-8") as f:
                for t in tables:
                    try:
                        cur.execute(f"SELECT * FROM {t}")
                        cols = [d[0] for d in cur.description or []]
                        for row in cur.fetchall():
                            line = json.dumps({"table": t, "cols": cols, "row": list(row)},
                                              ensure_ascii=False, default=str)
                            f.write(line + "\n")
                    except Exception as e:
                        f.write(json.dumps({"table": t, "error": str(e)},
                                           ensure_ascii=False) + "\n")
        finally:
            conn.close()


def _looks_like_sqlite(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            head = f.read(16)
        return head.startswith(b"SQLite format 3\x00")
    except OSError:
        return False
