"""DSH Work — AI 原生桌面工作台。

三层架构：
- UI 层 (dsh_work.ui)：PySide6 渲染、模式管理器、主题引擎
- 业务逻辑层 (dsh_work.core)：会话状态机、模式/技能加载、进程管理
- DSH 通信层 (dsh_work.api)：HTTP/WebSocket 客户端 + 版本适配器
"""

__version__ = "0.7.0"
__app_name__ = "DSH Work"
