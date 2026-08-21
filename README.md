# DSH Work

> AI 原生桌面工作台 · 基于 [DSH (DeepSeek Harness)](https://github.com/deepseek-ai/deepseek-harness) 的 Work / Code 双模式原生客户端。

为不熟悉 WebUI 的用户提供**一键安装、开箱即用**的桌面体验：无需预装 Python / Node.js / npm，首次启动自动下载便携运行时并拉起 DSH。v0.4.0 起，界面布局、配色、对话渲染全面对齐 DSH WebUI（dsh-web-frontend 的 `--dsw-*` 设计 token）。

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

> **English version: [README.en.md](./README.en.md)**

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

**界面与体验（v0.4.0 Web 版对齐）**
- **布局对齐 DSH WebUI**：AppFrame 三栏——左侧 Sidebar（会话/文件树/搜索/Git + 底部设置）、中央对话列（会话头 + 消息流 + 输入条）、右侧 Details（预览/工具调用，默认关闭）；顶栏含品牌、工作区、Agent 预设、模型选择、主题与设置入口
- **主题对齐 Web token**：内置 **Web Dark / Web Light** 两套主题，配色直接取自 dsh-web-frontend 的设计 token（品牌蓝 #4176E6 / #679EFE、neutral-bluish 中性色阶、markdown 代码块 token）；旧版主题名自动迁移
- **对话渲染对齐 Web ChatView**：全宽消息行（非气泡）——用户消息右对齐、Assistant 左对齐；自研 Markdown 渲染器支持代码块（语言标签条）、表格、行内代码、标题、引用、列表
- **对话逻辑对齐**：流式「思考中…/工具执行中…」状态提示、turn tail 统计（本轮输入/输出 tokens、费用）、工具调用折叠卡、会话状态点（running 蓝 / pending 琥珀 / done 绿）
- **Composer 输入条对齐**：Enter / Ctrl+Enter 发送、Shift+Enter 换行；底行快捷键提示 + 模型座 + 发送↔停止按钮；上下文容量条颜色编码（<70% 蓝 / 70-90% 橙 / >90% 红）
- 系统托盘 + 迷你浮窗：关闭即最小化到托盘，可选浮窗速览
- DeepSeek 余额内联小部件：对话底部实时显示「本轮 ¥X · 余额 ¥Y」，双通道容错（DSH 代理 + 平台直连），5 分钟缓存，点击强制刷新，余额不足警示色

**工程保障（v0.6.0 起）**
- **GitHub Actions CI/CD**：每次推送自动执行 ruff 静态检查 + 30 项单元测试（Windows 实机）；push main / 打 tag 时自动构建 PyInstaller + Inno Setup 安装包并上传 artifact；打 `v*` tag 即自动发布到 GitHub Release
- **单元测试门禁**：`tests/` 覆盖版本三源一致性（constants / pyproject / Inno Setup）、用户配置序列化与旧主题迁移、内置主题 JSON 结构、日志凭据脱敏——防止发布错版本与凭据泄漏
- **版本单一来源**：`dsh_work/constants.py` 的 `APP_VERSION` 与 `pyproject.toml`、`installer/dsh-work.iss` 由 CI 强制同步，任何漂移都会让流水线失败

---

## 📦 安装

### 方式一：终端用户（推荐）

下载 `DSHWork-Setup-0.6.0.exe`，双击安装即可，无需任何前置依赖。

- 安装目录：`%ProgramFiles%\DSHWork`
- 用户数据：`%USERPROFILE%\.dsh-work\`（配置、主题、日志、便携运行时，卸载默认保留）
- 静默安装：`DSHWork-Setup-0.6.0.exe /VERYSILENT /CURRENTUSER`

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
4. **开始对话**：左栏点 `＋ 新建会话`，中栏输入消息后按 `Enter` 发送（`Shift+Enter` 换行）

---

## 🏗️ 架构

三层隔离，任意一层可独立替换：

```
┌─────────────────────────────────────────────────┐
│  UI 层 (dsh_work/ui/)                           │
│  PySide6 AppFrame 三栏 · Web 主题 · 托盘 · 引导  │
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
│   ├── ui/                   # UI 层（Web 版 AppFrame）
│   │   ├── main_window.py    #   主窗口编排（TitleBar + Sidebar + 对话列 + Details + StatusBar）
│   │   ├── title_bar.py      #   顶栏（品牌/工作区/预设/模型/主题/设置）
│   │   ├── panels/           #   左栏 Sidebar / 中栏对话列 / 右栏 Details / 用量面板
│   │   ├── widgets/          #   消息流 / Markdown 渲染 / 输入条 / 会话头 / 工具卡
│   │   ├── theme/            #   Web 深浅主题管理 + QSS 生成
│   │   └── onboarding/       #   启动画面 + 场景引导
│   ├── resources/themes/     # 内置主题 JSON（web_dark / web_light）
│   ├── config.py             # 用户配置持久化
│   └── constants.py          # 全局常量
├── installer/                # 打包脚本
│   ├── dsh-work.iss          #   Inno Setup 安装程序（版本单一来源）
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
| `theme` | 主题名（`web_dark` / `web_light`） | `web_dark` |
| `workspace` | 工作区路径 | 空 |
| `panel_ratios` | 三栏宽度比例 | `{0.20, 0.58, 0.22}` |
| `panel_collapsed` | 面板折叠状态 | `{false, false}` |
| `minimize_to_tray` | 关闭最小化到托盘 | `true` |
| `custom_dsh_endpoint` | 自定义 DSH 端点（降级救急） | 空 |
| `check_updates` | 自动检查更新 | `true` |
| `readability_protection` | 可读性自动保护 | `true` |

> v0.3 及更早版本的主题名（`midnight_ocean` / `daylight` / `qinghua` / `forest_green`）会在加载时自动迁移到对应的 Web 深浅主题。

快捷键：`Ctrl+B` 切换左栏、`Ctrl+J` 切换右栏、`Enter` 发送 / `Shift+Enter` 换行、`Ctrl+Enter` 强制发送、`Esc` 停止、`Ctrl+,` 设置、`Ctrl+.` 切换主题。

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
- `installer\Output\DSHWork-Setup-0.6.0.exe` — Windows 安装包（版本号自动读取自 `installer\dsh-work.iss`，与 `dsh_work/constants.py` 的 `APP_VERSION` 同步）

---

## 🩺 故障排查

| 现象 | 原因与解决 |
|------|-----------|
| 启动画面卡住 | DSH 子进程首次下载/安装较慢，查看画面下方日志；超时点"导出诊断日志" |
| 消息发送后界面空白 | 检查 API Key 是否配置（设置 → 验证 Key）；网络/RPC 失败会自动回滚 |
| WebSocket 频繁重连 | DSH 端点不可达；尝试设置 → 自定义 DSH 端点 |
| 主题不符合预期 | 确认当前主题为 `web_dark` / `web_light`；旧主题名已自动迁移，无需手动修改配置 |
| 消息 Markdown 渲染异常 | 若代码块/表格显示异常，请附带示例消息反馈；渲染器位于 `ui/widgets/markdown_view.py` |
| 托盘图标缺失 | 系统托盘设置中开启 DSH Work 图标显示 |

日志位置：`<项目根>/logs/`（按大小轮转，保留 7 个文件）；诊断导出见帮助菜单。

---

## 📋 更新日志

### v0.6.0 — CI/CD 流水线与单元测试门禁

#### 工程保障

| 变更 | 说明 |
|------|------|
| GitHub Actions CI/CD | 新增 `.github/workflows/ci.yml`：lint（ruff）→ test（pytest 30 项，Windows 实机）→ package（PyInstaller onefile + Inno Setup 安装包，上传 artifact）；打 `v*` tag 自动发布到 GitHub Release（`softprops/action-gh-release`） |
| 单元测试骨架 | 新增 `tests/` 共 30 项：版本三源一致性（constants / pyproject / iss 强制同步）、`UserConfig` 序列化往返与损坏回退、旧主题名自动迁移（midnight_ocean 等）、自定义端点 http→ws / https→wss 映射、内置主题 JSON 结构与颜色值校验、日志 `SensitiveFilter` 凭据脱敏 |
| 版本号统一 0.6.0 | constants / `__init__` / pyproject / installer 四源一致，由 `tests/test_version_consistency.py` 在 CI 中强制门禁 |
| pytest 配置 | `pyproject.toml` 新增 `[tool.pytest.ini_options]`（testpaths / addopts / pythonpath） |

#### 新增/修改文件

```
新增：
.github/workflows/ci.yml        # lint + test + package + release 四段式流水线
tests/test_version_consistency.py  # 版本三源一致性（发布门禁）
tests/test_config.py               # 配置序列化/损坏回退/主题迁移/WS URL 映射
tests/test_theme_resources.py      # 内置主题 JSON 结构完整性
tests/test_logger_mask.py          # 日志凭据脱敏
修改：
pyproject.toml                 # version 0.6.0 + pytest 配置
dsh_work/constants.py          # APP_VERSION 0.6.0
dsh_work/__init__.py           # __version__ 0.6.0
installer/dsh-work.iss         # MyAppVersion 0.6.0
```

### v0.4.0 — Web 版界面整体重构

#### 界面与体验

| 变更 | 说明 |
|------|------|
| 布局对齐 DSH WebUI | 移除 TRAE 风格 ActivityBar 与中心 Tab 壳，改为 AppFrame 三栏：Sidebar（会话/文件树/搜索/Git，顶部导航 + 底部设置）/ 对话列（会话头 + 消息流 + 输入条）/ Details（预览，默认关闭） |
| 主题全面切换 Web token | 内置 Web Dark / Web Light 两套主题，配色取自 dsh-web-frontend 的 `--dsw-*` 设计 token；旧主题名（midnight_ocean / daylight / qinghua / forest_green）自动迁移 |
| 消息渲染对齐 Web ChatView | 全宽消息行（非气泡）：用户右对齐、Assistant 左对齐；自研 Markdown 渲染器（代码块语言标签条、表格、行内代码、标题、引用、列表） |
| 对话逻辑对齐 | 流式「思考中…/工具执行中…」提示、turn tail 统计（本轮输入/输出 tokens、费用）、工具调用折叠卡（Web ToolRow 样式）、会话状态点（running 蓝 / pending 琥珀 / done 绿） |
| Composer 输入条 | Enter / Ctrl+Enter 发送、Shift+Enter 换行（对齐 Web 键盘语义）；底行快捷键提示 + 模型座 + 发送↔停止按钮；上下文容量条 token 配色 |
| 顶栏 | 品牌 + 工作区 + Agent 预设 + 模型选择 + 主题切换 + 设置入口 |

#### 修复与工程

- 修复 `terminal_manager.py` 线程名引用未定义变量（启动终端会 NameError）
- 修复 `update_checker.py` f-string 反斜杠导致的 Python 3.11 语法错误
- ruff 全量清零（import 排序、% 格式化、死代码等历史问题）
- 版本号统一 0.4.0（constants / `__init__` / pyproject / installer / build 脚本单一来源）

#### 新增/删除文件

```
新增：
dsh_work/ui/widgets/conversation_header.py   # Web 版会话头（标题 + 视图切换 + 新建）
dsh_work/ui/widgets/markdown_view.py         # Web 版 Markdown 渲染器
dsh_work/resources/themes/web_dark.json      # Web 深色主题
dsh_work/resources/themes/web_light.json     # Web 浅色主题

删除：
dsh_work/ui/widgets/activity_bar.py          # TRAE 风格图标栏（被 Sidebar 顶部导航取代）
dsh_work/resources/themes/midnight_ocean.json / daylight.json / qinghua.json
```

### v0.5.0 — Web 版高级交互 + 桌面/Web 会话记录互通

#### 会话工作记录互通（桌面版 ↔ Web 版）

| 变更 | 说明 |
|------|------|
| 进程共存修复 | 端口被占且无 PID 文件时，先做 `host.describe` RPC 探测：健康 DSH（如 Web 版正在使用的实例）→ 复用而非误杀；只有探测失败才按崩溃孤儿清理。桌面版与 Web 版可同时连接同一 DSH |
| 会话列表互通 | Web 版新建/删除/改名会话，桌面版 15 秒定时 + WS 事件自动同步；修正 bootstrap 临时会话过滤（不再误隐藏 Web 侧创建的空白会话） |
| 工作区分组 | 会话列表按 `cwd` 工作区分组（对齐 Web 版 Workspace 分组），组内按更新时间倒序 |
| 完整工作记录读取 | 新增 `core/session_log.py`：直接解析 `~/.dsh/sessions/**/session.jsonl.zstd`（zstd 帧扫描 + 存储行展开），聚合消息/思考/工具/审批/计划/队列，切换会话时回放 |

#### Web 版高级交互

| 组件 | 说明 |
|------|------|
| Think 行 | 解析 `reasoning-delta` 思考流：默认折叠，实时摘要尾随，展开显示完整推理，TURN_END 后收拢为「已思考」 |
| Context 注入行 | `user/message` 且 `source.kind != user` 时折叠展示（上下文注入 / 跨会话召回 / 技能加载） |
| StatsDock 统计条 | 从 `session.list` 的 projections（sessionStats / contextPressure / tokenUsage）显示 Turn·Step、上下文占比、输入/输出 tokens、LLM/工具耗时 |
| TodoDock 计划条 | `todo/write` 事件 + projections.todos + jsonl 回放，折叠标题 + 状态计数（进行中/完成/待办） |
| QueueDock 队列条 | `agent/inbox/spliced` 事件，运行时显示排队消息（折叠/展开） |
| ApprovalPanel 审批条 | `approval/asked` 时 composer 上方显示琥珀警示条（工具名 + 原因）；响应通道说明见下 |

> **审批响应通道说明**：实测运行中的 DSH (0.0.1) 无审批相关 RPC（`approval.*` / `tools.submit` 等全部 method-not-found），按方案降级——审批条提供「在 Web 版中批准」（打开 WebUI 处理，DSH 升级支持审批 RPC 后自动接通）与「拒绝」（中断当前 Agent 执行）。

#### 工程

- `api/ws_client.py`：WSEventType 扩展（todo_write / approval_asked / approval_decided / approval_policy / queue_spliced / goal_change / user_message）与 DSH 事件映射
- `api/dsh_service.py`：`SessionInfo` 扩展 cwd/running/blank/projections；`get_full_events()` 返回完整事件流
- 版本号统一 0.5.0

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
