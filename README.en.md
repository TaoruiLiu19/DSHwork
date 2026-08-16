# DSH Work

> AI-native desktop workbench · A native Work / Code dual-mode client for [DSH (DeepSeek Harness)](https://github.com/deepseek-ai/deepseek-harness).

Built for users unfamiliar with the WebUI: **one-click install, works out of the box**. No need to preinstall Python / Node.js / npm — on first launch it automatically downloads a portable runtime and starts DSH, presenting all WebUI features in a native three-pane layout.

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

**UI & experience**
- Three-pane collapsible layout: left (sessions/file tree/search/git), center (message stream + input), right (preview); widths and collapse state are persisted
- Theme system: built-in Midnight Ocean / Daylight / Qinghua themes with frosted-glass texture and readability auto-protection
- System tray + mini floating window: closing minimizes to tray, optional floating quick-view
- DeepSeek balance inline widget: live "This turn ¥X · Balance ¥Y" at the bottom of the conversation; dual-channel fallback (DSH proxy + platform direct), 5-min cache, click to force refresh, warning color for low balance

---

## 📦 Installation

### Option 1: End users (recommended)

Download `DSHWork-Setup-x.y.z.exe` and double-click to install. No prerequisites required.

- Install directory: `%ProgramFiles%\DSHWork`
- User data: `%USERPROFILE%\.dsh-work\` (config, themes, logs, portable runtime; kept on uninstall by default)
- Silent install: `DSHWork-Setup-x.y.z.exe /VERYSILENT /CURRENTUSER`

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

1. **First launch**: a splash screen runs environment checks; if DSH is not present locally, the portable Node.js is downloaded and DSH is installed to `~/.dsh-work/runtime/` automatically
2. **Scenario picker**: on first launch you choose your usage scenario (Work / Code)
3. **Configure API Key**: Settings (top-right) → enter your DeepSeek API Key → click "Verify Key" for real-time validation
4. **Start chatting**: click `+` in the left pane to create a session, type in the center pane and press `Ctrl+Enter` to send

---

## 🏗️ Architecture

Three isolated layers, any of which can be replaced independently:

```
┌─────────────────────────────────────────────────┐
│  UI layer (dsh_work/ui/)                        │
│  PySide6 three-pane layout · themes · tray ·    │
│  onboarding                                     │
├─────────────────────────────────────────────────┤
│  Business logic layer (dsh_work/core/)          │
│  SessionManager · ProcessManager · OfflineCache │
│  UpdateChecker · DshDownloader                  │
├─────────────────────────────────────────────────┤
│  DSH communication layer (dsh_work/api/)        │
│  HttpClient · WsClient · VersionAdapter         │
│  ReconnectManager · DshService (facade)         │
└─────────────────────────────────────────────────┘
        │                          │
        │ HTTP (Typert RPC)        │ WebSocket (events.host / events.mux)
        ▼                          ▼
   127.0.0.1:3080  ← DSH subprocess (spawned by portable Node)
```

Communication protocol: DSH uses [Typert RPC](https://github.com/deepseek-ai/deepseek-harness) with HTTP endpoint `POST /api/<method>` and dual WebSocket streams carrying host events and session events respectively. On startup, the version adapter probes the DSH version at the raw protocol level and automatically selects the compatible mode.

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
│   │   └── reconnect.py      #   reconnect management (never dies)
│   ├── core/                 # business logic layer
│   │   ├── session_manager.py#   session state machine
│   │   ├── process_manager.py#   DSH subprocess lifecycle
│   │   ├── dsh_downloader.py #   portable Node + local DSH install
│   │   ├── update_checker.py #   auto-update check
│   │   └── offline_cache.py  #   offline SQLite cache
│   ├── ui/                   # UI layer
│   │   ├── main_window.py    #   main window orchestration
│   │   ├── panels/           #   left / center / right panes
│   │   ├── widgets/          #   message list / tool cards / input box
│   │   ├── theme/            #   theme management + frosted glass
│   │   └── onboarding/       #   splash screen + scenario picker
│   ├── resources/themes/     # built-in theme JSON
│   ├── config.py             # user config persistence
│   └── constants.py          # global constants
├── installer/                # packaging scripts
│   ├── dsh-work.iss          #   Inno Setup installer
│   └── build.ps1             #   one-click build (PyInstaller + ISCC)
├── dsh_work.spec             # PyInstaller packaging config
├── run.py                    # packaging entry (avoids relative-import issues)
└── pyproject.toml
```

---

## ⚙️ Configuration

User config file: `~/.dsh-work/config.json`

| Key | Description | Default |
|-----|-------------|---------|
| `mode` | Work mode `work` / `code` | `work` |
| `theme` | Theme name | `midnight_ocean` |
| `workspace` | Workspace path | empty |
| `panel_ratios` | Three-pane width ratios | `{0.18, 0.58, 0.24}` |
| `panel_collapsed` | Pane collapse state | `{false, false}` |
| `minimize_to_tray` | Minimize to tray on close | `true` |
| `custom_dsh_endpoint` | Custom DSH endpoint (fallback for recovery) | empty |
| `check_updates` | Auto-check for updates | `true` |
| `readability_protection` | Readability auto-protection | `true` |

Keyboard shortcuts: `Ctrl+B` toggle left pane, `Ctrl+J` toggle right pane, `Ctrl+Enter` send message.

---

## 🔨 Building

Prerequisites: Python 3.11+, [Inno Setup 6](https://jrsoftware.org/isdl.php)

| Command | Description |
|---------|-------------|
| `.\installer\build.ps1` | One-click build (PyInstaller onedir + Inno Setup installer) |
| `.\installer\build.ps1 -SkipInstaller` | Package exe only, skip the installer |
| `.\installer\build.ps1 -DebugConsole` | Debug mode (with console window) |

Artifacts:
- `dist\DSHWork\` — PyInstaller onedir directory
- `installer\Output\DSHWork-Setup-0.3.0.exe` — Windows installer

---

## 🩺 Troubleshooting

| Symptom | Cause & solution |
|---------|------------------|
| Splash screen hangs | First download/install of the DSH subprocess is slow; check the log at the bottom of the splash; click "Export diagnostics log" on timeout |
| Blank UI after sending a message | Check whether the API Key is configured (Settings → Verify Key); network/RPC failures roll back automatically |
| WebSocket keeps reconnecting | DSH endpoint unreachable; try Settings → Custom DSH endpoint |
| Blurred theme text | Disable frosted glass or enable "Readability auto-protection" |
| Missing tray icon | Enable DSH Work icon in the system tray settings |

Log location: `~/.dsh-work/logs/` (daily rotation, kept for 7 days)

---

## 📋 Changelog

### v0.3.0

#### New features

| Feature | Description |
|---------|-------------|
| DeepSeek balance inline widget | Live "This turn ¥X · Balance ¥Y" at the bottom of the conversation; dual-channel fallback (DSH proxy + platform direct), 5-min cache, click to force refresh, warning color for low balance |
| External vision model tool `inspect_image` | Send local/URL images to any OpenAI-compatible vision endpoint (qwen-vl / GLM-4V / Ollama); client-side downsampling saves tokens; results auto-feed back to the agent for further reasoning |
| File change tracking + one-click restore | Baseline snapshot + unified line-level diff + per-file/all restore; conflict protection (refuses restore when content hash mismatches); recoverable after crash/restart |
| VSCode-style file tree + HTML/port preview | Lazy-loaded directory tree, hidden-file toggle, substring search; cross-platform port enumeration (psutil / netstat / ss / lsof); local static preview server (key-based mapping, path-traversal protection) |
| In-session PowerShell SSE streaming terminal | Per-session subprocess, UTF-8 clean Chinese output, persisted command history (up/down), auto-reconnect restoring CWD, 30ms output throttling |
| Dual auto-update | Official dsh: npm registry dual-source detection + atomic upgrade + automatic rollback on failure; client: GitHub/Gitee release dual-source takes the higher version, 8MB×6 concurrent sharded download + SHA256 verification |
| One-click migration | Auto-detects Codex / Claude Code directories; migrates skills (timestamp suffix on name conflicts), MCP config (normalized format, merged write), and memory (SQLite with extra JSONL export) |

#### New files

```
dsh_work/
├── paths.py                     # cross-platform path helpers (user_data / cache / update / migrations)
├── tools/
│   ├── __init__.py
│   └── inspect_image.py         # external vision model tool
├── core/
│   ├── file_tracker.py          # file change tracking + one-click restore
│   ├── file_tree.py             # VSCode-style file tree + port management + HTML preview service
│   ├── terminal_manager.py      # in-session PowerShell SSE streaming terminal
│   ├── update_orchestrator.py   # dual auto-update coordinator
│   └── migration.py             # one-click migration (Codex/Claude Code → DSH Work)
└── resources/themes/
    └── qinghua.json              # Qinghua (blue-and-white) theme (replaces forest_green.json)
```

#### Modified files

| File | Change summary |
|------|---------------|
| `config.py` | Added vision model config items (vision_api_base / vision_api_key / vision_model / vision_max_image_size / vision_default_prompt) |
| `core/session_manager.py` | Added per-turn cost tracking, TOOL_CALL local tool interception, file-change tracking API, TURN_END auto-scan |
| `ui/main_window.py` | Theme switching loops all themes, settings panel follows theme colors, localization, balance widget integration |
| `ui/theme/theme_manager.py` | Built-in theme mapping forest_green → qinghua |
| `ui/widgets/balance_widget.py` | Balance inline widget UI component |
| `ui/panels/center_panel.py` | Integrated BalanceWidget into layout |
| `constants.py` | APP_VERSION 0.1.0 → 0.3.0 |
| `__init__.py` | \_\_version\_\_ 0.1.0 → 0.3.0 |

---

## 🙏 Acknowledgements

Some features of this project are designed and implemented with inspiration from the community project [Deepseek-Harness-EAC](https://github.com/zouyuxuan122/Deepseek-Harness-EAC). We sincerely thank the original author **zouyuxuan122** and all contributors.

| Referenced feature | Original implementation | Adaptation in this project |
|--------------------|-------------------------|----------------------------|
| External vision model tool | OpenAI-compatible vision endpoint (qwen-vl/GLM-4V/Ollama) | PySide6 native tool-call card + streaming integration |
| In-session terminal | PowerShell SSE streaming terminal | QProcess + SSE stream, command history / auto-reconnect / Chinese encoding |
| VSCode-style file tree | Electron built-in file tree + preview | QTreeView + right-pane HTML/port preview subpanel |
| File change tracking | Line-level diff + one-click restore | Workspace snapshot + exact content match replace + conflict protection |
| Balance inline widget | "This turn ¥X · Balance ¥Y" at conversation bottom | Qt widget + dual-channel balance query fallback |
| Dual auto-update | Official dsh npm overlay + client self-update | GitHub/Gitee dual-source + sharded merge rollback |
| One-click migration tool | Codex / Claude Code → skills/MCP/memory | Directory scan + format adaptation + conflict wizard |

If the original project updates or you need referenced content removed, please let us know via an Issue and we will handle it promptly.

---

## 📄 License

MIT