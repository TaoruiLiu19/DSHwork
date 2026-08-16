# DSH Work

> AI 原生桌面工作台 · 基于 [DSH (DeepSeek Harness)](https://github.com/deepseek-ai/deepseek-harness) 的 Work / Code 双模式原生客户端。

为不熟悉 WebUI 的用户提供**一键安装、开箱即用**的桌面体验：无需预装 Python / Node.js / npm，首次启动自动下载便携运行时并拉起 DSH，所有 WebUI 功能以原生三栏布局呈现。

- **技术栈**：Python 3.11+ · PySide6 6.6+ · requests · websockets · SQLite
- **平台**：Windows（Inno Setup 安装包 │ PyInstaller 打包）
- **协议**：Typert RPC（HTTP）+ 双 WebSocket 事件流

---

## 📑 目录

- [✨ 核心特性](#-核心特性)
- [📦 安装](#-安装)
- [🚀 快速开始](#-快速开始)
- [🏗️ 架构](#️-架构)
- [📁 目录结构](#-目录结构)
- [⚙️ 配置](#️-配置)
- [🔨 构建打包](#-构建打包)
- [🩺 故障排查](#-故障排查)
- [📋 更新日志](#-更新日志)
- [🙏 致谢](#-致谢)
- [📄 许可证](#-许可证)

---

## ✨ 核心特性

**开箱即用**
- 傻瓜式安装：Inno Setup 安装包 + 便携 Node.js 运行时，终端用户双击即用
- 离线降级：DSH 不可用时进入离线模式，历史缓存可读；重连后增量恢复
- 自动更新：官方 dsh（npm 原子切换 + 回退）+ 客户端自更新（GitHub/Gitee 双源 + 分片合并 + SHA256 校验）

**对话核心**
- WebUI 功能对齐：会话管理、流式消息、工具调用卡片、文件预览/Diff、附件上传、模型切换
- Work / Code 双模式：Work 偏成果展示，Code 偏代码工程，状态栏一键切换
- 完整错误反馈：API Key 缺失/失效、RPC 失败均有可操作弹窗（"打开设置"按钮），乐观 UI 失败自动回滚

**生产力工具**
- 会话内终端：PowerShell SSE 流式终端，命令历史 / 断线重连 / 中文编码干净
- VSCode 风格文件树 + HTML/端口预览：懒加载目录树、端口枚举、本地静态预览服务
- 文件更改追踪 + 一键还原：行级 diff + 逐文件/全部还原（内容精确匹配后替换，冲突保护）
- 一键迁移：从 Codex / Claude Code 目录自动迁移 skills / MCP / 记忆
- 外置视觉模型工具：`inspect_image` 将本地/URL 图片发给任意 OpenAI 兼容视觉端点（qwen-vl / GLM-4V / Ollama），结果自动回传 Agent 继续推理

**界面与体验**
- 三栏可折叠布局：左栏（会话/文件树/搜索/Git）、中栏（消息流+输入）、右栏（预览），宽度与折叠状态持久化
- 主题系统：内置午夜海洋 / 日光 / 青花三套主题，支持磨砂玻璃质感与可读性自动保护
- 系统托盘 + 迷你浮窗：关闭即最小化到托盘，可选浮窗速览
- DeepSeek 余额内联小部件：对话底部实时显示「本轮 ¥X · 余额 ¥Y」，双通道容错（DSH 代理 + 平台直连），5 分钟缓存，点击强制刷新，余额不足警示色

---

## 📦 安装

### 方式一：终端用户（推荐）

下载 `DSHWork-Setup-x.y.z.exe`，双击安装即可，无需任何前置依赖。

- 安装目录：`%ProgramFiles%\DSHWork`
- 用户数据：`%USERPROFILE%\.dsh-work\`（配置、主题、日志、便携运行时，卸载默认保留）
- 静默安装：`DSHWork-Setup-x.y.z.exe /VERYSILENT /CURRENTUSER`

### 方式二：源码运行（开发者）

```bash
git clone <repo-url>
cd DSHWork
pip install -e .
dsh-work                # 或 python -m dsh_work.main
```

依赖：Python 3.11+、PySide6 6.6+、requests、websockets、portalocker、PyYAML

---

## 🚀 快速开始

1. **首次启动**：显示启动画面做环境检测；若本地无 DSH，自动下载便携 Node.js 并安装 DSH 到 `~/.dsh-work/runtime/`
2. **场景选择**：首次启动引导选择使用场景（Work / Code）
3. **配置 API Key**：右上角设置 → 填入 DeepSeek API Key → 点击"验证 Key"实时校验
4. **开始对话**：左栏点 `+` 新建会话，中栏输入消息 `Ctrl+Enter` 发送

---

## 🏗️ 架构

三层隔离，任意一层可独立替换：

```
┌─────────────────────────────────────────────────┐
│  UI 层 (dsh_work/ui/)                           │
│  PySide6 三栏布局 · 主题 · 托盘 · 引导          │
├─────────────────────────────────────────────────┤
│  业务逻辑层 (dsh_work/core/)                    │
│  SessionManager · ProcessManager · OfflineCache │
│  UpdateChecker · DshDownloader                  │
├─────────────────────────────────────────────────┤
│  DSH 通信层 (dsh_work/api/)                     │
│  HttpClient · WsClient · VersionAdapter         │
│  ReconnectManager · DshService(facade)          │
└─────────────────────────────────────────────────┘
        │                          │
        │ HTTP (Typert RPC)        │ WebSocket (events.host / events.mux)
        ▼                          ▼
   127.0.0.1:3080  ← DSH 子进程（便携 Node 拉起）
```

通信协议：DSH 使用 [Typert RPC](https://github.com/deepseek-ai/deepseek-harness)，HTTP 端点 `POST /api/<method>`，双 WebSocket 流分别承载主机事件与会话流。版本适配器在启动时裸协议探测 DSH 版本，自动选择兼容模式。

---

## 📁 目录结构

```
DSHWork/
├── dsh_work/
│   ├── api/                  # DSH 通信层
│   │   ├── dsh_service.py    #   对外 facade
│   │   ├── http_client.py    #   HTTP (Typert RPC)
│   │   ├── ws_client.py      #   WebSocket 双流
│   │   ├── version_adapter.py#   版本探测与兼容降级
│   │   └── reconnect.py      #   重连管理（降级不死）
│   ├── core/                 # 业务逻辑层
│   │   ├── session_manager.py#   会话状态机
│   │   ├── process_manager.py#   DSH 子进程生命周期
│   │   ├── dsh_downloader.py #   便携 Node + 本地 DSH 安装
│   │   ├── update_checker.py #   自动更新检查
│   │   └── offline_cache.py  #   离线 SQLite 缓存
│   ├── ui/                   # UI 层
│   │   ├── main_window.py    #   主窗口编排
│   │   ├── panels/           #   左中右三栏
│   │   ├── widgets/          #   消息流/工具卡/输入框
│   │   ├── theme/            #   主题管理 + 磨砂玻璃
│   │   └── onboarding/       #   启动画面 + 场景引导
│   ├── resources/themes/     # 内置主题 JSON
│   ├── config.py             # 用户配置持久化
│   └── constants.py          # 全局常量
├── installer/                # 打包脚本
│   ├── dsh-work.iss          #   Inno Setup 安装程序
│   └── build.ps1             #   一键构建（PyInstaller + ISCC）
├── dsh_work.spec             # PyInstaller 打包配置
├── run.py                    # 打包入口（避免相对导入问题）
└── pyproject.toml
```

---

## ⚙️ 配置

用户配置文件：`~/.dsh-work/config.json`

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `mode` | 工作模式 `work` / `code` | `work` |
| `theme` | 主题名 | `midnight_ocean` |
| `workspace` | 工作区路径 | 空 |
| `panel_ratios` | 三栏宽度比例 | `{0.18, 0.58, 0.24}` |
| `panel_collapsed` | 面板折叠状态 | `{false, false}` |
| `minimize_to_tray` | 关闭最小化到托盘 | `true` |
| `custom_dsh_endpoint` | 自定义 DSH 端点（降级救急） | 空 |
| `check_updates` | 自动检查更新 | `true` |
| `readability_protection` | 可读性自动保护 | `true` |

快捷键：`Ctrl+B` 切换左栏、`Ctrl+J` 切换右栏、`Ctrl+Enter` 发送消息。

---

## 🔨 构建打包

需预先安装：Python 3.11+、[Inno Setup 6](https://jrsoftware.org/isdl.php)

| 命令 | 说明 |
|------|------|
| `.\installer\build.ps1` | 一键构建（PyInstaller onedir + Inno Setup 安装包） |
| `.\installer\build.ps1 -SkipInstaller` | 仅打包 exe，跳过安装程序 |
| `.\installer\build.ps1 -DebugConsole` | 调试模式（带控制台窗口） |

产物：
- `dist\DSHWork\` — PyInstaller onedir 目录
- `installer\Output\DSHWork-Setup-0.3.0.exe` — Windows 安装包

---

## 🩺 故障排查

| 现象 | 原因与解决 |
|------|-----------|
| 启动画面卡住 | DSH 子进程首次下载/安装较慢，查看画面下方日志；超时点"导出诊断日志" |
| 消息发送后界面空白 | 检查 API Key 是否配置（设置 → 验证 Key）；网络/RPC 失败会自动回滚 |
| WebSocket 频繁重连 | DSH 端点不可达；尝试设置 → 自定义 DSH 端点 |
| 主题文字模糊 | 关闭磨砂玻璃或开启"可读性自动保护" |
| 托盘图标缺失 | 系统托盘设置中开启 DSH Work 图标显示 |

日志位置：`~/.dsh-work/logs/`（按天轮转，保留 7 天）

---

## 📋 更新日志

### v0.3.0

#### 新增功能

| 功能 | 说明 |
|------|------|
| DeepSeek 余额内联小部件 | 对话底部实时显示「本轮 ¥X · 余额 ¥Y」，双通道容错（DSH 代理 + 平台直连），5 分钟缓存，点击强制刷新，余额不足警示色 |
| 外置视觉模型工具 `inspect_image` | 把本地/URL 图片发给任意 OpenAI 兼容视觉端点（qwen-vl / GLM-4V / Ollama），客户端侧下采样省 token，结果自动回传 Agent 继续推理 |
| 文件更改追踪 + 一键还原 | 基线快照 + 行级统一 diff + 逐文件/全部还原，冲突保护（内容 hash 不匹配时拒绝还原），崩溃重启可恢复 |
| VSCode 风格文件树 + HTML/端口预览 | 懒加载目录树、隐藏文件切换、子串搜索；端口枚举（psutil / netstat / ss / lsof 跨平台）；本地静态预览服务（按 key 映射，路径越界保护） |
| 会话内 PowerShell SSE 流式终端 | 每会话独立子进程，UTF-8 环境中文不乱码，命令历史持久化（上下切换），断线自动重连恢复 CWD，30ms 输出节流 |
| 双重自动更新 | 官方 dsh：npm registry 双源检测 + 原子升级 + 失败自动回退；客户端：GitHub/Gitee release 双源取更高版本，8MB×6 并发分片下载 + SHA256 校验 |
| 一键迁移 | 自动探测 Codex / Claude Code 目录，迁移 skills（同名冲突加时间戳）、MCP 配置（格式统一规范化，合并写入）、记忆（SQLite 额外导出 JSONL） |

#### 新增文件

```
dsh_work/
├── paths.py                     # 跨平台路径助手（user_data / cache / update / migrations）
├── tools/
│   ├── __init__.py
│   └── inspect_image.py         # 外置视觉模型工具
├── core/
│   ├── file_tracker.py          # 文件更改追踪 + 一键还原
│   ├── file_tree.py             # VSCode 风格文件树 + 端口管理 + HTML 预览服务
│   ├── terminal_manager.py      # 会话内 PowerShell SSE 流式终端
│   ├── update_orchestrator.py   # 双重自动更新协调器
│   └── migration.py             # 一键迁移（Codex/Claude Code → DSH Work）
└── resources/themes/
    └── qinghua.json              # 青花国风主题（替换 forest_green.json）
```

#### 修改文件

| 文件 | 变更摘要 |
|------|---------|
| `config.py` | 新增视觉模型配置项（vision_api_base / vision_api_key / vision_model / vision_max_image_size / vision_default_prompt） |
| `core/session_manager.py` | 新增本轮消耗追踪、TOOL_CALL 本地工具拦截执行、文件变更追踪 API、TURN_END 自动扫描 |
| `ui/main_window.py` | 主题切换循环所有主题、设置面板跟随主题变色、中文化、余额小部件串联 |
| `ui/theme/theme_manager.py` | 内置主题映射 forest_green → qinghua |
| `ui/widgets/balance_widget.py` | 余额内联小部件 UI 组件 |
| `ui/panels/center_panel.py` | 集成 BalanceWidget 到布局 |
| `constants.py` | APP_VERSION 0.1.0 → 0.3.0 |
| `__init__.py` | \_\_version\_\_ 0.1.0 → 0.3.0 |

---

## 🙏 致谢

本项目的部分功能设计与实现灵感参考了 [Deepseek-Harness-EAC](https://github.com/zouyuxuan122/Deepseek-Harness-EAC) 社区项目，在此向原作者 **zouyuxuan122** 及所有贡献者致以诚挚感谢。

| 参考功能 | 原项目实现方式 | 本项目适配说明 |
|---------|---------------|---------------|
| 外置视觉模型工具 | OpenAI 兼容视觉端点（qwen-vl/GLM-4V/Ollama） | PySide6 原生工具调用卡片 + 流式集成 |
| 会话内终端 | PowerShell SSE 流式终端 | QProcess + SSE 流，命令历史 / 断线重连 / 中文编码 |
| VSCode 风格文件树 | Electron 内置文件树 + 预览 | QTreeView + 右栏 HTML/端口预览子面板 |
| 文件更改追踪 | 行级 diff + 一键还原 | 工作区快照 + 精确内容匹配替换 + 冲突保护 |
| 余额内联小部件 | 对话底部「本轮 ¥X · 余额 ¥Y」 | Qt widget + 双通道余额查询容错 |
| 双重自动更新 | 官方 dsh npm overlay + 客户端自更新 | GitHub/Gitee 双源 + 分片合并回退 |
| 一键迁移工具 | Codex / Claude Code → skills/MCP/记忆 | 目录扫描 + 格式适配 + 冲突向导 |

若原项目有更新或需要移除相关参考内容，请通过 Issue 告知，我们会第一时间处理。

---

## 📄 许可证

MIT