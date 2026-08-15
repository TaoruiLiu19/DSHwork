"""UI 渲染层 (PySide6)。

组件：
- main_window: 主窗口，三栏布局
- mode_manager: Work/Code 模式切换控件
- title_bar: 顶部工具栏（模式切换开关、模型选择器、设置）
- status_bar: 底部状态栏（连接状态、Agent 状态、上下文容量、Token/余额、DSH 版本）
- system_tray: 系统托盘集成
- widgets: 消息流、输入框、工具调用卡片、空状态卡片、内联预览
- panels: 左栏（任务/文件）、中栏（对话/工作区）、右栏（预览/工具）
- onboarding: 启动画面、场景选择引导
- settings: 设置面板
"""

from .main_window import MainWindow

__all__ = ["MainWindow"]
