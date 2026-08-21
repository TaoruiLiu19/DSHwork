"""DSH Work 全局常量。

所有硬编码的端点、路径片段、超时、阈值集中在此，避免散落各处。
"""

from __future__ import annotations

# ===== 应用元信息 =====
APP_NAME = "DSH Work"
APP_VERSION = "0.7.0"
ORG_NAME = "DSH Work"
ORG_DOMAIN = "dsh.work"

# ===== DSH loopback 端点 =====
# DSH Web API 默认在 127.0.0.1:3080 启动（dsh --profile web）
DSH_DEFAULT_HOST = "127.0.0.1"
DSH_DEFAULT_PORT = 3080
DSH_BASE_URL = f"http://{DSH_DEFAULT_HOST}:{DSH_DEFAULT_PORT}"
# DSH Typert RPC: POST /api/<method.name>，body 是 client-request 信封；这里只保留 URL 前缀基址
DSH_API_PREFIX = "/api"
# DSH WebSocket 端点（实际协议：events.host 承载主机事件，events.mux 承载多路复用会话流）
DSH_WS_URL = f"ws://{DSH_DEFAULT_HOST}:{DSH_DEFAULT_PORT}"
DSH_WS_HOST_EVENTS_PATH = "/api/events.host"
DSH_WS_MUX_EVENTS_PATH = "/api/events.mux"
# 浏览器信任围栏：请求 /api 必须携带合法 Origin（Host 头校验同源）
DSH_ORIGIN_HEADER = DSH_BASE_URL

# 版本适配器裸协议探测的候选 RPC 方法名（不是 URL 路径！）
# 调用方式是 POST /api/<method> + Typert client-request 信封
ADAPTER_PROBE_METHODS = [
    "host.describe",      # value.version
    "settings.describe",  # 备选，若 host.describe 失败
    "credentials.describe",
]

# 适配器缓存文件名（存放于 ~/.dsh-work/）
ADAPTER_CACHE_FILENAME = ".adapter_cache.json"

# ===== DSH CLI / 进程 =====
DSH_CLI_PACKAGE = "@deepseek-ai/dsh"
DSH_CLI_COMMAND = "dsh"
DSH_WEB_SUBCOMMAND = "web"
DSH_PID_FILENAME = ".dsh.pid"
DSH_STARTUP_TIMEOUT_SEC = 180  # DSH 子进程启动超时（npx首次、沙箱环境、中文路径cwd等都可能使启动 > 60s）
DSH_CLI_INSTALL_TIMEOUT_SEC = 600  # 首次自动安装 DSH CLI 的超时（npm install -g，依赖较多需 5min+）
NPM_REGISTRY_MIRROR = "https://registry.npmmirror.com"  # 国内镜像加速 npm install

# Node.js 最低版本要求
NODE_MIN_VERSION_MAJOR = 18

# ===== 外部 API（余额查询降级通道）=====
DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEEPSEEK_BALANCE_PATH = "/user/balance"
BALANCE_REFRESH_INTERVAL_SEC = 300  # 5 分钟

# ===== WebSocket 重连 =====
WS_RECONNECT_BASE_DELAY_SEC = 1.0
WS_RECONNECT_MAX_DELAY_SEC = 30.0
WS_RECONNECT_MAX_ATTEMPTS = 10
WS_HEARTBEAT_INTERVAL_SEC = 30

# ===== 上下文容量阈值（第 9.2 节）=====
CONTEXT_WARN_THRESHOLD = 0.80   # ≥ 80% 输入框下方浅色提示
CONTEXT_DANGER_THRESHOLD = 0.95  # ≥ 95% 发送按钮警示态 + 确认
# 颜色编码：< 70% 蓝，70-90% 橙，> 90% 红
CONTEXT_COLOR_SEGMENTS = [
    (0.70, "accent"),
    (0.90, "warning"),
    (1.01, "error"),
]

# ===== 内置模型上下文长度映射（DSH 未暴露 context_length 时回退）=====
MODEL_CONTEXT_LENGTH_FALLBACK = {
    "deepseek-chat": 64000,
    "deepseek-reasoner": 64000,
    "deepseek-coder": 128000,
}
DEFAULT_CONTEXT_LENGTH = 64000

# ===== 离线缓存 =====
OFFLINE_DB_FILENAME = "offline_cache.db"
OFFLINE_CACHE_MAX_MESSAGES = 5000  # LRU 淘汰上限

# ===== 性能优化阈值（第 8.5 节）=====
VIRTUALIZATION_MSG_THRESHOLD = 200       # 消息流虚拟化触发
WS_BATCH_INTERVAL_MS = 16                # chunk 事件批处理
SESSION_LAZY_LOAD_THRESHOLD = 100        # 会话历史懒加载
SESSION_LAZY_LOAD_BATCH = 50             # 每次追加加载条数

# 主题实时预览限频
THEME_PREVIEW_VIEWPORT_LIMIT = 20        # 拖动滑块时只刷新可见前 N 条
THEME_PREVIEW_SETTLE_MS = 300            # 松开滑块后全量刷新延迟

# 空闲降频刷新
IDLE_THRESHOLD_SEC = 300                 # 5 分钟无新事件
IDLE_REFRESH_INTERVAL_SEC = 10           # 降频后刷新间隔

# ===== 主题刷新限频 =====
THEME_APPLY_DEBOUNCE_MS = 50

# ===== 进程管理 =====
PROCESS_POLL_INTERVAL_SEC = 2

# ===== HTTP 客户端 =====
HTTP_TIMEOUT_SEC = 30
HTTP_POOL_CONNECTIONS = 10
HTTP_POOL_MAXSIZE = 10

# ===== 模式定义 =====
MODE_WORK = "work"
MODE_CODE = "code"

# ===== 自动更新检查（P3-4）=====
# 留空则不检查。支持两种远端格式：
# 1. GitHub Releases API（返回 JSON 含 tag_name/html_url/body）：
#    https://api.github.com/repos/<owner>/<repo>/releases/latest
# 2. 自定义 JSON：{"version":"x.y.z","download_url":"...","release_notes":"..."}
UPDATE_CHECK_URL = ""
UPDATE_CHECK_TIMEOUT_SEC = 10
# 启动后延迟检查（毫秒），避免与 DSH 初始化抢资源
UPDATE_CHECK_DELAY_MS = 4000

# ===== 便携运行时（不依赖系统 Node.js / npm，P3-2）=====
# 首次需要时下载官方便携 Node.js 到 ~/.dsh-work/runtime/，再用其内置 npm
# 把 @deepseek-ai/dsh 装到同目录，启动用 [node, dsh入口js] 直接拉起。
RUNTIME_DIRNAME = "runtime"
PORTABLE_NODE_VERSION = "v24.19.0"           # Node 24 LTS (Krypton)；dsh 需 createZstdDecompress (v22.15+/v23+)
NODE_DOWNLOAD_TIMEOUT_SEC = 180               # 便携 Node 下载超时（zip ~30MB）
DSH_LOCAL_INSTALL_TIMEOUT_SEC = 600           # 本地 npm install dsh 超时
NODE_DIST_BASE = "https://nodejs.org/dist"    # 官方分发站点

# ===== 日志 =====
LOG_DIRNAME = "logs"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB 单文件
LOG_BACKUP_COUNT = 7             # 保留 7 天

# ===== 主题 =====
THEMES_DIRNAME = "themes"
BUILTIN_THEMES = ["web_dark", "web_light"]
DEFAULT_THEME = "web_dark"

# ===== 用户配置文件名 =====
USER_CONFIG_FILENAME = "config.json"

# ===== 内联预览 =====
INLINE_PREVIEW_MAX_HEIGHT_PX = 400
