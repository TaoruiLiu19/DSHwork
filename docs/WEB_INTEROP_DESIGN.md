# 方案设计：Web 版高级交互实现 + 桌面版/Web 版会话工作记录互通

> 版本：v1（待确认） · 日期：2026-08-17 · 状态：**方案待确认，未改任何代码**

---

## 一、调研结论（本轮实测，非推断）

### 1.1 数据源：DSH 会话日志是"唯一权威工作记录"

实测解压 `~/.dsh/sessions/**/session.jsonl.zstd`，确认 **Web 版高级交互所需的全部事件都已持久化**，桌面版与 Web 版共享同一份：

| 事件类型 | 用途 | Web 版对应组件 |
|---|---|---|
| `assistant/chunk`（含 `blockType: "reasoning"` 块）、`assistant/message`（含 `content: [{type:"reasoning",text}]`） | 思考过程 | **Think 行**（折叠） |
| `todo/write`（`todos:[{content,status}]`） | 计划 | **TodoDock 计划条** |
| `approval/policy`、`approval/asked`（id/toolName/callId/reason）、`approval/decided`（outcome） | 审批 | **ApprovalPanel 审批条** |
| `agent/inbox/spliced`（target:next-turn, inserted） | 排队消息/steer | **QueueDock 队列条** |
| `user/message`（`source.kind != "user"` 时为注入/召回） | 上下文注入 | **Context 注入行** |
| `turn/start`、`step/start`、`request/context`（contextWindow）、`request/header`（model） | 统计 | **StatsDock 统计条** |
| `tool/call`、`tool/result`、`permission/preset`、`sandbox/mode`、`session/title` | 基础工作记录 | 消息流/会话列表 |

> 关键结论：**"会话工作记录互通"在数据层已经成立**——Web 与桌面连同一个 DSH（127.0.0.1:3080），所有记录由 DSH 写入 `~/.dsh/sessions/`，双方共享。缺的是桌面侧的读取/展示能力与进程共存保障。

### 1.2 实测：运行中的 DSH（版本 0.0.1）RPC 能力边界

对 127.0.0.1:3080 实测探测（Typert RPC）：

- ✅ 可用：`session.create / list / history / models / updateQueue`、`host.describe`、`settings.describe`、`credentials.describe`
- ❌ `method-not-found`：`session.summary / projection / state / queue / steer / stats / context`、`todo.*`、`goal.*`、`approval.*`、`tools.submit`、`session.submitToolResult` 等（**审批/队列/计划没有 RPC**）

结论：高级交互（审批/计划/队列）的数据在**事件层**（WS + jsonl），不在 RPC 层；审批响应等**客户端→DSH 的控制通道需实测确认**（见风险 §5.1）。

### 1.3 进程共存现状

`process_manager.py` 已具备"复用用户手动启动的 DSH"逻辑（PID 文件存在且 owner 非本客户端时复用），但存在一个误杀路径：

> 端口 3080 被占且**无 PID 文件**（用户手动 `dsh --profile web` 启动、Web 版正在用）→ 当前判定为"崩溃孤儿"→ **直接 taskkill 杀掉** → Web 版断连。

这是互通的**首要障碍**，必须修复（§3.1）。

---

## 二、目标与范围

### 2.1 目标
1. **互通**：桌面版与 Web 版可同时连接同一 DSH，互不干扰；任一侧创建的会话、消息、工具调用、审批、计划，另一侧打开即可见（最终一致性，DSH 为单一事实源）。
2. **高级交互**：桌面版补齐 Web 版的 6 项高级交互（Think 行 / TodoDock / QueueDock / ApprovalPanel / Context 行 / StatsDock），视觉与逻辑对齐 Web。

### 2.2 明确不做的（互通边界）
以下属于**桌面版本地状态**，不互通、也不需要互通：
- 离线缓存（`offline_cache.db`）、用量统计（`usage_tracker`）、文件追踪基线（`file_tracker`）
- 三栏宽度/折叠、主题、输入框草稿等 UI 状态
- 本地草稿会话（`local-*`，DSH 离线降级产物，恢复后回写 DSH 才可见）

---

## 三、会话工作记录互通方案

### 3.1 修复进程共存（首要，Phase 1）

**改动点**：`process_manager.py` 端口占用判定（约 L651-668）

| 场景 | 现状 | 改后 |
|---|---|---|
| 端口占 + 无 PID 文件 | 判定孤儿 → 杀掉 | 先 `host.describe` **RPC 探测**：成功 → 视为"外部 DSH"复用（写 owner=external 的 PID 锁）；失败 → 才判定孤儿清理 |
| 端口占 + PID 文件 + owner=本客户端 | 复用 | 复用（不变） |
| 端口占 + PID 文件 + owner=用户/外部 | 复用 | 复用（不变，已有） |

退出时：仅当 `is_dsh_owned`（本客户端启动的）才 stop_dsh；外部 DSH 只断开连接不杀进程（现有 `is_dsh_owned` 判断已具备，需在复用外部 DSH 时置 False）。

### 3.2 会话列表互通（Phase 1）

- 实时性：监听 WS `session/created`、`session/deleted` 事件 + 保留 3 秒级 `refresh_sessions` 轮询（已有），Web 侧新建会话桌面侧自动出现。
- **跨客户端会话元数据**：`session.history` 对空会话返回空，但 Web 侧可能创建了"只有标题无消息"的会话——当前 `list_sessions` 会过滤掉"无标题且无消息"（bootstrap 过滤），需调整过滤条件：仅过滤**本客户端创建的 bootstrap 会话**（通过会话 id 前缀或维护本地集合），其余全部展示。
- 工作区目录分组：`session.list` 已返回 `cwd`/`updatedAt`/`running` 等字段，桌面侧会话列表可加"按工作区分组"（对齐 Web 版 Workspace 分组），Data 源已具备。

### 3.3 完整工作记录读取（Phase 1 基础能力）

新增 `dsh_work/core/session_log.py`（复用 `session_watcher` 的 zstd 帧扫描/解压）：

- 输入：sessionId → 定位 `~/.dsh/sessions/<cwd-encoded>/<sessionId>/session.jsonl.zstd`
- 输出：结构化记录（turn 列表 → messages / reasoning / tool calls / approvals / todos / queue splices），供：
  - 会话详情完整展示（含 Web 侧产生的高级事件）
  - Think/Todo/Queue/Approval 的**历史回放**（切换会话时立即显示，不等实时事件）
- 增量能力复用 `SessionWatcher` 的尾部消费逻辑（`FileRecord.consumed`），不重复读盘。

---

## 四、高级交互实现方案

所有交互的数据源均为 **WS 实时事件 + jsonl 历史回放** 双通道（实时增量、切换会话回放），视觉对齐 Web token。

### 4.1 Think 行（思考过程折叠）— 风险低
- 数据：`assistant/chunk` 中 `blockType=="reasoning"` 的 `text-delta` 块；`assistant/message` 中 `content[{type:"reasoning"}]`（历史回放）
- UI：消息流中用户/助手之间插入可折叠行「🧠 思考中…（实时摘要尾随）」，收起显示首行摘要，展开显示完整推理；TURN_END 后折叠为「已思考」
- 复用：现有 `MessageRow.set_status_hint` 机制扩展为独立 `ThinkRow`（QSS 已有 `ThinkRow` 样式）

### 4.2 Context 注入行（上下文来源折叠）— 风险低
- 数据：`user/message` 且 `source.kind != "user"`（如 `injection`/`recall`/`skill`），或 `system` 类消息
- UI：折叠行，头部显示来源角色（「上下文注入」/「跨会话召回」+ 来源名），展开显示内容（对齐 Web `DisclosureRow`，141px 高度上限）

### 4.3 StatsDock（统计条）— 风险低
- 数据：`turn/start`（turn 号）、`step/start/end`（step 计数）、`request/context`（contextWindow 容量）、`TOKEN_USAGE`（已有）
- UI：composer 上方细条：「Turn N · Step M · 上下文 X% · 本轮输入/输出 tokens」，颜色分段（<70% 蓝 / 70-90% 橙 / >90% 红）
- 与现有 `BalanceWidget` 并列，可合并为一行（桌面版已显示余额，Web 版没有余额条，按桌面版增强处理）

### 4.4 TodoDock（计划条）— 风险低
- 数据：WS `todo/write` 事件 + jsonl 回放（取"最新一次 todo/write 且其后再无 turn/start"为当前计划，对齐 Web 语义）
- UI：composer 上方可折叠计划条：「📋 计划 · 3 进行中 · 2 完成」，点击展开列表，每项状态点 + 文案（截断一行）

### 4.5 QueueDock（队列条）— 风险中
- 数据：`agent/inbox/spliced`（target=next-turn）事件 + `session.updateQueue`（发送侧已有）
- UI：Agent 运行时，composer 上方显示队列条：单条直接展示、多条折叠「N 条排队消息」，支持展开/删除（删除走 `session.cancel` 或 RPC 待实测）
- 风险：`inbox/spliced` 的精确结构（inserted 内容块格式）需实机验证

### 4.6 ApprovalPanel（审批面板）— 风险高 ⚠️
- 数据：`approval/policy`（策略 ask）→ `approval/asked`（id/toolName/callId/reason）→ `approval/decided`（outcome）
- UI：**composer 整体接管为审批条**（对齐 Web）：琥珀色警示条 + 工具名 + 原因 + 「允许一次」/「拒绝」按钮；等待期间输入框只读
- 响应通道（客户端→DSH）：当前 DSH (0.0.1) **无审批 RPC**；候选通道：
  1. WS `events.mux` 发送审批响应帧（Web 版协议，需抓包确认帧格式）
  2. 权限预设 RPC（`settings.describe` 有 `permission` 命名空间，可能存在 `permission.set` 类方法，需探测）
  3. **降级方案**：若上述通道在 0.0.1 均不可用 → 桌面版显示审批通知 + 引导「该操作需在 Web 版中批准」（在 Web 版可用时体验一致；同时把审批状态醒目展示）

### 4.7 与现有组件的整合
- 所有新行/条都挂在现有消息流/Composer 布局中，复用现有主题 QSS（`ThinkRow`/`ToolRow`/`ContextRow`/`StatsDock` 样式已内置）
- `session_manager` 扩展 WS 事件解析：`reasoning` 块、`todo/write`、`approval/*`、`agent/inbox/spliced`
- 切换会话时：先 jsonl 回放历史（Think/Todo/Queue/Approval/Context），再实时增量

---

## 五、风险与开放问题

| # | 风险 | 影响 | 对策 |
|---|---|---|---|
| 1 | **审批响应通道未确认**（0.0.1 无审批 RPC；WS 帧格式需抓包） | ApprovalPanel 可能无法真正"批准" | Phase 3 前置一个 15 分钟的实机验证（触发一次真实审批抓包）；不通则走降级方案（§4.6-3），并如实标注"0.0.1 支持度" |
| 2 | DSH 版本差异（runtime 包 0.1.0-rc.6 vs 运行中 0.0.1） | 升级后 RPC/事件可能变化 | 版本适配器已预留降级；事件解析按"结构探测"写（缺失字段不崩溃），不做版本硬编码 |
| 3 | `agent/inbox/spliced`、reasoning 块结构细节 | QueueDock/ThinkRow 微调 | 实现时以真实会话日志样本为准（已确认事件存在，结构已见样例） |
| 4 | 双端同时操作同一会话（并发 steer/审批） | 状态竞争 | DSH 是唯一事实源，客户端只渲染；冲突以 DSH 侧判定为准（与 Web 版行为一致） |
| 5 | 多开桌面版实例 | 双客户端争抢 | 维持现有 PID 锁语义：第二实例复用同一 DSH（不杀），UI 各自独立 |

---

## 六、分阶段实施计划

| 阶段 | 内容 | 交付物 | 验收标准 |
|---|---|---|---|
| **P1 互通基础** | 进程共存修复（RPC 探测复用外部 DSH）；bootstrap 过滤调整；`session_log.py` jsonl 读取器；会话列表工作区分组 | 桌面版+Web 版同时在线互不干扰；Web 新建会话桌面可见；历史会话完整记录可读 | 双端同开 30 分钟无断连；交叉创建会话即时可见 |
| **P2 对话高级交互** | Think 行、Context 行、StatsDock（WS 解析扩展 + jsonl 回放） | 三组件上线 | 与 Web 版对照截图一致；切换会话历史回放正确 |
| **P3 计划/队列/审批** | TodoDock、QueueDock、ApprovalPanel（前置审批通道实机验证） | 三组件上线 | 审批可真实放行/拒绝（或走降级）；计划/队列实时刷新 |
| **P4 收尾** | 全量回归（ruff + 编译 + 冒烟）、README 更新、版本号 0.5.0 | 发布 | 与 v0.4 功能无回归 |

工作量估计：P1 约 30%，P2 约 25%，P3 约 30%，P4 约 15%。整体风险集中在 P3 的审批通道。

---

## 七、待你确认的决策点

1. **范围**：按 P1→P4 全做，还是先做某几个阶段？（建议全做，P1/P2 无风险先落地）
2. **审批通道**：同意"先实机验证、不通则降级为通知+引导到 Web 版批准"的策略吗？（0.0.1 的限制不以桌面端能力为代价硬扛）
3. **统计条位置**：StatsDock 与现有余额条合并一行，还是独立一行？（建议合并，紧凑）
4. **计划/队列/审批的 UI 完整度**：与 Web 版 1:1（含队列编辑/steer 细节），还是先做核心（显示+折叠+审批按钮）？（建议先核心，交互细节后续迭代）
5. **版本号**：完成后升 0.5.0？（建议是）
