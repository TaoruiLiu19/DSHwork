# DSH Work

> AI-native desktop workbench · A native Work / Code dual-mode client for [DSH (DeepSeek Harness)](https://github.com/deepseek-ai/deepseek-harness).

Built for users unfamiliar with the WebUI: **one-click install, works out of the box**. No need to preinstall Python / Node.js / npm — on first launch it automatically downloads a portable runtime and starts DSH. Since v0.4.0 the UI layout, colors, and conversation rendering are fully aligned with the DSH WebUI (the `--dsw-*` design tokens from dsh-web-frontend).

- **Tech stack**: Python 3.11+ · PySide6 6.6+ · requests · websockets · SQLite
- **Platform**: Windows (Inno Setup installer │ PyInstaller packaging)
- **Protocol**: Typert RPC (HTTP) + dual WebSocket event streams

---

## 📑 Table of Contents

- [✨ Key Features](#-key-features)
- [📦 Installation](#-installation)
- [🚀 Quick Start](#-quick-start)
- [🏗️ Architecture](#️-architecture)
- [📁 Directory Structure](#-directory-structure)
- [⚙️ Configuration](#️-configuration)
- [🔨 Building](#-building)
- [🩺 Troubleshooting](#-troubleshooting)
- [📋 Changelog](#-changelog)
- [🙏 Acknowledgements](#-acknowledgements)
- [📄 License](#-license)

> **中文版请见 [README.md](./README.md)** · **English version: this file (README.en.md)**

---

## ✨ Key Features

**Out of the box**
- Frictionless install: Inno Setup installer + portable Node.js runtime — end users just double-click
- Offline degradation: enters offline mode when DSH is unavailable; cached history stays readable; incremental recovery on reconnect
- Auto-update: official dsh (npm atomic switch + rollback) + client self-update (GitHub/Gitee dual-source + sharded merge + SHA256 verification)

**Conversation core**
- WebUI feature parity: session management, streaming messages, tool-call cards, file preview/diff, attachment upload, model switching
- Work / Code dual mode: Work favors deliverables, Code favors engineering; one-click switch in the status bar
- Complete error feedback: actionable popups for missing/invalid API Key and RPC failures (with an "Open Settings" button); optimistic UI rolls back automatically on failure

**Productivity tools**
- In-session terminal: PowerShell SSE streaming terminal with command history / auto-reconnect / clean Chinese encoding
- VSCode-style file tree + HTML/port preview: lazy-loaded directory tree, port enumeration, local static preview server
- File change tracking + one-click restore: line-level diff + per-file/all restore (exact content match then replace, with conflict protection)
- One-click migration: auto-migrate skills / MCP / memory from Codex or Claude Code directories
- External vision model tool: `inspect_image` sends local/URL images to any OpenAI-compatible vision endpoint (qwen-vl / GLM-4V / Ollama), auto-feeding results back to the agent for further reasoning

**UI & experience (v0.4.0 Web-aligned)**
- **Layout aligned with DSH WebUI**: AppFrame three-pane — left Sidebar (sessions/file tree/search/git + bottom settings), center conversation column (session header + message stream + composer), right Details (preview/tool calls, closed by default); top bar holds brand, workspace, agent preset, model selector, theme and settings entries
- **Themes aligned with Web tokens**: built-in **Web Dark / Web Light** themes, colors taken directly from dsh-web-frontend design tokens (brand blue #4176E6 / #679EFE, neutral-bluish scale, markdown code-block tokens); legacy theme names migrate automatically
- **Conversation rendering aligned with Web ChatView**: full-width message rows (not bubbles) — user messages right-aligned, assistant messages left-aligned; a built-in Markdown renderer supports code blocks (with language banner), tables, inline code, headings, quotes, and lists
- **Conversation logic aligned**: streaming "Thinking… / Running tool…" status hints, turn-tail stats (this-turn input/output tokens and cost), collapsible tool rows, session status dots (running blue / pending amber / done green)
- **Composer aligned**: Enter / Ctrl+Enter to send, Shift+Enter for a newline; bottom row shows shortcut hints + model seat + send↔stop button; context usage bar color-coded (<70% blue / 70–90% amber / >90% red)
- System tray + mini floating window: closing minimizes to tray, optional floating quick-view
- DeepSeek balance inline widget: live "This turn ¥X · Balance ¥Y" at the bottom of the conversation; dual-channel fallback (DSH proxy + platform direct), 5-min cache, click to force refresh, warning color for low balance

**Engineering guarantees (since v0.6.0)**
- **GitHub Actions CI/CD**: every push runs ruff lint + 30 unit tests (on real Windows runners); pushes to main / tags automatically build the PyInstaller + Inno Setup installer and upload it as an artifact; pushing a `v*` tag publishes it to a GitHub Release automatically
- **Unit-test gate**: `tests/` covers version single-source consistency (constants / pyproject / Inno Setup), `UserConfig` serialization and legacy theme migration, built-in theme JSON structure, and log credential masking — preventing wrong-version releases and credential leaks
- **Version single source**: `APP_VERSION` in `dsh_work/constants.py` is forced in sync with `pyproject.toml` and `installer/dsh-work.iss` by CI; any drift fails the pipeline

---

## 📦 Installation

### Option 1: End users (recommended)

Download `DSHWork-Setup-0.6.0.exe` and double-click to install. No prerequisites required.

- Install directory: `%ProgramFiles%\DSHWork`
- User data: `%USERPROFILE%\.dsh-work\` (config, themes, logs, portable runtime; kept on uninstall by default)
- Silent install: `DSHWork-Setup-0.6.0.exe /VERYSILENT /CURRENTUSER`

### Option 2: Run from source (developers)

```bash
git clone <repo-url>
cd DSHWork
pip install -e .
dsh-work                # or python -m dsh_work.main
```

Dependencies: Python 3.11+, PySide6 6.6+, requests, websockets, portalocker, PyYAML

---

## 🚀 Quick Start

1. **First launch**: a splash screen runs environment checks; if DSH is not installed locally, it downloads a portable Node.js and installs DSH into `~/.dsh-work/runtime/`
2. **Scenario picker**: on first run, choose your primary use case (Work / Code)
3. **Configure API Key**: Settings (top right) → enter your DeepSeek API Key → click "Verify Key" for real-time validation
4. **Start chatting**: click `＋ New Session` in the left sidebar, type in the center column, and press `Enter` to send (`Shift+Enter` for a newline)

---

## 🏗️ Architecture

Three layers, each independently replaceable:

```
┌─────────────────────────────────────────────────┐
│  UI layer (dsh_work/ui/)                        │
│  PySide6 AppFrame · Web themes · tray · splash  │
├─────────────────────────────────────────────────┤
│  Business logic (dsh_work/core/)                │
│  SessionManager · ProcessManager · OfflineCache │
│  UpdateChecker · DshDownloader                  │
├─────────────────────────────────────────────────┤
│  DSH communication (dsh_work/api/)              │
│  HttpClient · WsClient · VersionAdapter         │
│  ReconnectManager · DshService (facade)         │
└─────────────────────────────────────────────────┘
        │                          │
        │ HTTP (Typert RPC)        │ WebSocket (events.host / events.mux)
        ▼                          ▼
   127.0.0.1:3080  ← DSH subprocess (spawned with portable Node)
```

Protocol: DSH uses [Typert RPC](https://github.com/deepseek-ai/deepseek-harness) — HTTP endpoint `POST /api/<method>` plus two WebSocket streams for host events and session streams. A version adapter probes the DSH version on startup and selects a compatible mode automatically.

---

## 📁 Directory Structure

```
DSHWork/
├── dsh_work/
│   ├── api/                  # DSH communication layer
│   │   ├── dsh_service.py    #   public facade
│   │   ├── http_client.py    #   HTTP (Typert RPC)
│   │   ├── ws_client.py      #   dual WebSocket streams
│   │   ├── version_adapter.py#   version probe & compatibility fallback
│   │   └── reconnect.py      #   reconnect manager (never dies)
│   ├── core/                 # business logic layer
│   │   ├── session_manager.py#   session state machine
│   │   ├── process_manager.py#   DSH subprocess lifecycle
│   │   ├── dsh_downloader.py #   portable Node + local DSH install
│   │   ├── update_checker.py #   update checks
│   │   └── offline_cache.py  #   offline SQLite cache
│   ├── ui/                   # UI layer (Web-style AppFrame)
│   │   ├── main_window.py    #   window orchestration (TopBar + Sidebar + Conversation + Details + StatusBar)
│   │   ├── title_bar.py      #   top bar (brand/workspace/preset/model/theme/settings)
│   │   ├── panels/           #   left sidebar / center conversation / right details / usage panel
│   │   ├── widgets/          #   message stream / markdown renderer / composer / conversation header / tool cards
│   │   ├── theme/            #   Web dark/light theme manager + QSS generation
│   │   └── onboarding/       #   splash screen + scenario picker
│   ├── resources/themes/     # built-in theme JSON (web_dark / web_light)
│   ├── config.py             # user config persistence
│   └── constants.py          # global constants
├── installer/                # packaging scripts
│   ├── dsh-work.iss          #   Inno Setup installer (single source of version)
│   └── build.ps1             #   one-click build (PyInstaller + ISCC)
├── dsh_work.spec             # PyInstaller config
├── run.py                    # packaging entry (avoids relative-import issues)
└── pyproject.toml
```

---

## ⚙️ Configuration

User config file: `~/.dsh-work/config.json`

| Key | Description | Default |
|-----|-------------|---------|
| `mode` | Working mode `work` / `code` | `work` |
| `theme` | Theme name (`web_dark` / `web_light`) | `web_dark` |
| `workspace` | Workspace path | empty |
| `panel_ratios` | Three-pane width ratios | `{0.20, 0.58, 0.22}` |
| `panel_collapsed` | Panel collapse state | `{false, false}` |
| `minimize_to_tray` | Minimize to tray on close | `true` |
| `custom_dsh_endpoint` | Custom DSH endpoint (fallback) | empty |
| `check_updates` | Auto-check for updates | `true` |
| `readability_protection` | Readability auto-protection | `true` |

> Legacy theme names from v0.3 and earlier (`midnight_ocean` / `daylight` / `qinghua` / `forest_green`) are automatically migrated to the corresponding Web theme on load.

Shortcuts: `Ctrl+B` toggle left panel, `Ctrl+J` toggle right panel, `Enter` send / `Shift+Enter` newline, `Ctrl+Enter` force send, `Esc` stop, `Ctrl+,` settings, `Ctrl+.` cycle theme.

---

## 🔨 Building

Prerequisites: Python 3.11+, [Inno Setup 6](https://jrsoftware.org/isdl.php)

| Command | Description |
|---------|-------------|
| `.\installer\build.ps1` | One-click build (PyInstaller onedir + Inno Setup installer) |
| `.\installer\build.ps1 -SkipInstaller` | Build the exe only, skip the installer |
| `.\installer\build.ps1 -DebugConsole` | Debug build (with console window) |

Artifacts:
- `dist\DSHWork\` — PyInstaller onedir directory
- `installer\Output\DSHWork-Setup-0.6.0.exe` — Windows installer (version is read from `installer\dsh-work.iss`, kept in sync with `APP_VERSION` in `dsh_work/constants.py`)

---

## 🩺 Troubleshooting

| Symptom | Cause & fix |
|---------|-------------|
| Splash screen hangs | DSH subprocess first download/install is slow; watch the log at the bottom; click "Export diagnostics" on timeout |
| Blank conversation after sending | Check that the API Key is configured (Settings → Verify Key); network/RPC failures roll back automatically |
| WebSocket reconnects constantly | DSH endpoint unreachable; try Settings → custom DSH endpoint |
| Theme looks unexpected | Make sure the active theme is `web_dark` / `web_light`; legacy names migrate automatically |
| Markdown renders oddly | If code blocks/tables look wrong, report a sample message; the renderer lives at `ui/widgets/markdown_view.py` |
| Tray icon missing | Enable the DSH Work icon in the system tray settings |

Logs: `<project root>/logs/` (size-rotated, 7 files kept); a diagnostics bundle is available from the help menu.

---

## 📋 Changelog

### v0.6.0 — CI/CD pipeline and unit-test gate

#### Engineering

| Change | Description |
|--------|-------------|
| GitHub Actions CI/CD | Added `.github/workflows/ci.yml`: lint (ruff) → test (30 pytest tests on real Windows runners) → package (PyInstaller onefile + Inno Setup installer, uploaded as artifact); pushing a `v*` tag auto-publishes to a GitHub Release (`softprops/action-gh-release`) |
| Unit-test skeleton | Added `tests/` with 30 tests: version single-source consistency (constants / pyproject / iss enforced in sync), `UserConfig` round-trip serialization and corrupt-file fallback, legacy theme name migration (midnight_ocean etc.), custom endpoint http→ws / https→wss mapping, built-in theme JSON structure & color-value validation, and `SensitiveFilter` credential masking |
| Version unified to 0.6.0 | constants / `__init__` / pyproject / installer kept in sync, enforced by `tests/test_version_consistency.py` in CI |
| pytest config | Added `[tool.pytest.ini_options]` to `pyproject.toml` (testpaths / addopts / pythonpath) |

#### Added/modified files

```
Added:
.github/workflows/ci.yml        # lint + test + package + release pipeline
tests/test_version_consistency.py  # version single-source gate
tests/test_config.py               # config serialization/corrupt fallback/theme migration/WS URL mapping
tests/test_theme_resources.py      # built-in theme JSON structure
tests/test_logger_mask.py          # log credential masking
Modified:
pyproject.toml                 # version 0.6.0 + pytest config
dsh_work/constants.py          # APP_VERSION 0.6.0
dsh_work/__init__.py           # __version__ 0.6.0
installer/dsh-work.iss         # MyAppVersion 0.6.0
```

### v0.4.0 — Web-style UI overhaul

#### UI & experience

| Change | Description |
|--------|-------------|
| Layout aligned with DSH WebUI | Removed the TRAE-style ActivityBar and the center tab shell; now an AppFrame with three panes: Sidebar (sessions/files/search/git, top nav + bottom settings) / Conversation column (session header + message stream + composer) / Details (preview, closed by default) |
| Themes switched to Web tokens | Built-in Web Dark / Web Light themes with colors from dsh-web-frontend `--dsw-*` tokens; legacy theme names migrate automatically |
| Message rendering aligned with Web ChatView | Full-width message rows (no bubbles): user right-aligned, assistant left-aligned; built-in Markdown renderer (code blocks with language banner, tables, inline code, headings, quotes, lists) |
| Conversation logic aligned | Streaming "Thinking… / Running tool…" hints, turn-tail stats (input/output tokens and cost), collapsible tool rows (Web ToolRow style), session status dots (running blue / pending amber / done green) |
| Composer input bar | Enter / Ctrl+Enter send, Shift+Enter newline (Web keyboard semantics); bottom row with shortcut hints + model seat + send↔stop; context bar token-colored |
| Top bar | Brand + workspace + agent preset + model selector + theme toggle + settings entry |

#### Fixes & engineering

- Fixed `terminal_manager.py` referencing an undefined variable in thread names (NameError when starting the terminal)
- Fixed `update_checker.py` f-string backslash, a Python 3.11 syntax error
- `ruff check` fully clean (import sorting, `%` formatting, dead code, and other legacy issues)
- Version unified to 0.4.0 (constants / `__init__` / pyproject / installer / build script single source)

#### Files added / removed

```
Added:
dsh_work/ui/widgets/conversation_header.py   # Web session header (title + view switch + new session)
dsh_work/ui/widgets/markdown_view.py         # Web Markdown renderer
dsh_work/resources/themes/web_dark.json      # Web dark theme
dsh_work/resources/themes/web_light.json     # Web light theme

Removed:
dsh_work/ui/widgets/activity_bar.py          # TRAE-style icon rail (replaced by sidebar top nav)
dsh_work/resources/themes/midnight_ocean.json / daylight.json / qinghua.json
```

### v0.5.0 — Web-style advanced interactions + desktop/Web session interoperability

#### Session record interoperability (desktop ↔ Web)

| Change | Description |
|--------|-------------|
| Process coexistence fix | When the port is taken without a PID file, probe `host.describe` first: a healthy DSH (e.g. the instance the Web version is using) is reused instead of killed; only probe failures are treated as orphans to clean up. Desktop and Web can now connect to the same DSH at the same time |
| Session list sync | Sessions created/renamed/deleted on the Web side appear automatically in the desktop sidebar (15s poll + WS events); fixed the bootstrap-session filter so blank sessions created by the Web side are no longer hidden |
| Workspace grouping | Session list groups by `cwd` (aligned with Web Workspace grouping), newest activity first |
| Full record reader | New `core/session_log.py` parses `~/.dsh/sessions/**/session.jsonl.zstd` directly (zstd frame scan + storage-row expansion), aggregating messages / reasoning / tool calls / approvals / todos / queue splices, replayed when switching sessions |

#### Web-style advanced interactions

| Component | Description |
|-----------|-------------|
| Think row | Parses `reasoning-delta` streams: collapsed by default with a live first-line summary, expand to read full reasoning; collapses to "Thought" at turn end |
| Context row | `user/message` with `source.kind != user` renders as a collapsible disclosure (context injection / cross-session recall / skill load) |
| StatsDock | Reads `session.list` projections (sessionStats / contextPressure / tokenUsage): Turn·Step, context %, input/output tokens, LLM/tool wall time |
| TodoDock | `todo/write` events + projections.todos + jsonl replay; collapsed header with status counts (in progress / completed / pending) |
| QueueDock | `agent/inbox/spliced` events; shows queued messages while the agent is running (collapsible) |
| ApprovalPanel | On `approval/asked`, an amber warning strip (tool name + reason) appears above the composer; response channel note below |

> **Approval response channel note**: the running DSH (0.0.1) exposes no approval RPC (all `approval.*` / `tools.submit` probes returned method-not-found), so per the agreed fallback the strip offers "Approve in Web" (opens the WebUI to handle it; auto-works once DSH adds the approval RPC) and "Deny" (cancels the current agent execution).

#### Engineering

- `api/ws_client.py`: WSEventType extended (todo_write / approval_asked / approval_decided / approval_policy / queue_spliced / goal_change / user_message) with DSH event mapping
- `api/dsh_service.py`: `SessionInfo` extended with cwd/running/blank/projections; added `get_full_events()` for the raw event stream
- Version unified to 0.5.0

### v0.3.0

#### New features

| Feature | Description |
|---------|-------------|
| DeepSeek balance inline widget | Live "This turn ¥X · Balance ¥Y" at the bottom of the conversation; dual-channel fallback (DSH proxy + platform direct), 5-min cache, click to force refresh, warning color for low balance |
| External vision tool `inspect_image` | Sends local/URL images to any OpenAI-compatible vision endpoint (qwen-vl / GLM-4V / Ollama); client-side downsampling saves tokens; results feed back to the agent automatically |
| File change tracking + one-click restore | Baseline snapshots + unified line diffs + per-file/all restore with conflict protection (refuses restore when content hash mismatches); survives crash restarts |
| VSCode-style file tree + HTML/port preview | Lazy-loaded directory tree, hidden-file toggle, substring search; port enumeration (psutil / netstat / ss / lsof, cross-platform); local static preview server (key-mapped, path-traversal guarded) |
| In-session PowerShell SSE streaming terminal | One subprocess per session, UTF-8 clean Chinese output, persisted command history (up/down), auto-reconnect restoring CWD, 30ms output throttling |
| Dual auto-update | Official dsh: npm dual-registry detection + atomic upgrade + rollback on failure; client: GitHub/Gitee release dual-source, 8MB×6 concurrent sharded download + SHA256 verification |
| One-click migration | Auto-detects Codex / Claude Code directories and migrates skills (timestamped on name conflict), MCP config (normalized and merged), and memory (SQLite plus JSONL export) |

---

## 🙏 Acknowledgements

Parts of this project's design and implementation are inspired by the community project [Deepseek-Harness-EAC](https://github.com/zouyuxuan122/Deepseek-Harness-EAC). We thank the original author **zouyuxuan122** and all contributors.

| Referenced feature | Original implementation | Adaptation here |
|--------------------|------------------------|-----------------|
| External vision tool | OpenAI-compatible vision endpoint (qwen-vl/GLM-4V/Ollama) | Native PySide6 tool-call card + streaming integration |
| In-session terminal | PowerShell SSE streaming terminal | QProcess + SSE stream, history / reconnect / clean encoding |
| VSCode-style file tree | Electron file tree + preview | QTreeView + right-pane HTML/port preview subpanel |
| File change tracking | Line-level diff + restore | Workspace snapshot + exact-content-match replace + conflict protection |
| Balance inline widget | "This turn ¥X · Balance ¥Y" | Qt widget + dual-channel balance query fallback |
| Dual auto-update | Official dsh npm overlay + client self-update | GitHub/Gitee dual-source + sharded merge fallback |
| One-click migration | Codex / Claude Code → skills/MCP/memory | Directory scan + format adaptation + conflict wizard |

If the original project is updated or you would like referenced content removed, please let us know via an Issue.

---

## 📄 License

MIT
