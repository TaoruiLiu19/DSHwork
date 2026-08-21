"""客户端本地工具集合。

DSH 原生工具调用通过 WebSocket 路由；此处提供客户端侧的"外置工具"：
当 Agent（模型）请求调用 inspect_image 等工具时，由客户端本地执行，
再把执行结果通过 TOOL_RESULT 事件回传给 DSH（从而让 Agent 能看到结果继续推理）。

支持的工具：
  - inspect_image: 把本地/URL 图片发给任意 OpenAI 兼容视觉端点（qwen-vl/GLM-4V/Ollama）
"""

from .inspect_image import (
    InspectImageResult,
    InspectImageTool,
    inspect_image_async,
    inspect_image_sync,
)

__all__ = [
    "InspectImageTool",
    "InspectImageResult",
    "inspect_image_sync",
    "inspect_image_async",
]
