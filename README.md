# DSH Work

> AI 原生桌面工作台 · 基于 [DSH (DeepSeek Harness)](https://github.com/deepseek-ai/deepseek-harness) 的 Work/Code 双模式原生客户端。

为不熟悉 WebUI 的用户提供**一键安装、开箱即用**的桌面体验：无需预装 Python / Node.js / npm，首次启动自动下载便携运行时并拉起 DSH，所有 WebUI 功能以原生三栏布局呈现。

---

## ✨ 核心特性

- **傻瓜式安装**：Inno Setup 安装包 + 便携 Node.js 运行时，终端用户双击即用
- **WebUI 功能对齐**：会话管理、流式消息、工具调用卡片、文件预览/Diff、附件上传、模型切换
- **Work / Code 双模式**：Work 模式偏成果展示，Code 模式偏代码工程，状态栏一键切换
- **三栏可折叠布局**：左栏（会话/文件树/搜索/Git）、中栏（消息流+输入）、右栏（预览），宽度与折叠状态持久化
- **离线降级**：DSH 不可用时进入离线模式，历史缓存可读；重连后增量恢复
- **主题系统**：内置午夜海洋 / 日光 / 森林三套主题，支持磨砂玻璃质感与可读性自动保护
- **系统托盘 + 迷你浮窗**：关闭即最小化到托盘，可选浮窗速览
- **自动更新检查**：启动后台静默拉取最新版本，有新版弹窗提示（可关闭/跳过）
- **完整错误反馈**：API Key 缺失/失效、RPC 失败均有可操作弹窗（"打开设置"按钮），乐观 UI 失败自动回滚

---

## 📦 安装

### 方式一：终端用户（推荐）

下载 `DSHWork-Setup-x.y.z.exe`，双击安装即可。无需任何前置依赖。

- 安装目录：`%ProgramFiles%\DSHWork`
- 用户数据：`%USERPROFILE%\.dsh-work\`（配置、主题、日志、便携运行时，卸载默认保留）
- 支持静默安装：`DSHWork-Setup-x.y.z.exe /VERYSILENT /CURRENTUSER`

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

### 一键构建（推荐）

需预先安装：Python 3.11+、[Inno Setup 6](https://jrsoftware.org/isdl.php)

```powershell
.\installer\build.ps1
```

产物：
- `dist\DSHWork\` — PyInstaller onedir 目录
- `installer\Output\DSHWork-Setup-0.1.0.exe` — Windows 安装包

### 仅打包 exe（跳过安装程序）

```powershell
.\installer\build.ps1 -SkipInstaller
```

### 调试模式（带控制台窗口）

```powershell
.\installer\build.ps1 -DebugConsole
```

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

## 📄 许可证

MIT
