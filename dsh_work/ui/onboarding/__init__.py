"""首次使用体验（第 7 章）。

启动流程四阶段：
1. 环境检测（后台自动，3-8秒）：Node.js / DSH CLI / DSH 运行中 / API Key / 可用模型
2. 场景选择（用户交互，10秒内）："你今天想做什么？"
3. 自动配置（后台，1-2秒）：根据场景设置模式 + 填充示例提示词
4. 开始工作（直接进入主窗口）
"""

from .splash_screen import SplashScreen
from .scenario_picker import ScenarioPicker

__all__ = ["SplashScreen", "ScenarioPicker"]
