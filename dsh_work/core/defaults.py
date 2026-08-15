"""硬编码默认技能、系统提示词、空状态卡片、场景引导卡片。

设计原则六「轻量优先」：首版不做技能系统、不做模板库、不做 JSON 解析器、不做管理面板。
默认技能硬编码在 Python 代码中，模式切换时直接引用。
用户侧的"自定义"入口缩减为空状态下的三个示例卡片——点击即填充提示词到输入框。

这不是一个"系统"，只是一组常量。如果将来需要自定义，用户可以直接改这个文件。
"""

from __future__ import annotations

# ===== 硬编码默认技能（第 5.2 节）=====
# 每个"技能"本质上就是一段系统提示词。模式切换时，模式管理器直接读取对应模式的默认提示词，
# 拼接到会话的系统消息中。

DEFAULT_SKILLS = {
    "work": [
        {
            "name": "文档撰写",
            "system_prompt": (
                "你是一位专业的文档撰写助手。"
                "输出使用 Markdown 格式，结构清晰，"
                "包含标题层级和列表。"
            ),
        },
        {
            "name": "数据分析",
            "system_prompt": (
                "你是一位数据分析专家。"
                "处理数据时先理解结构，"
                "再给出分析和建议。"
            ),
        },
        {
            "name": "演示文稿",
            "system_prompt": (
                "你是一位演示文稿设计顾问。"
                "输出结构化的幻灯片大纲。"
            ),
        },
    ],
    "code": [
        {
            "name": "代码审查",
            "system_prompt": (
                "你是一位严格的代码审查员。"
                "关注安全、性能和可读性，"
                "给出具体的改进建议。"
            ),
        },
        {
            "name": "调试辅助",
            "system_prompt": (
                "你是一位调试助手。"
                "分析错误信息，定位问题根因，"
                "给出修复方案。"
            ),
        },
        {
            "name": "Git 工作流",
            "system_prompt": (
                "你是一位 Git 工作流助手。"
                "帮助管理分支、生成提交信息、"
                "解决合并冲突。"
            ),
        },
    ],
}

# ===== 模式默认系统提示词（第 2.3 节）=====
# Work 模式：引导式，偏向解释和建议
# Code 模式：技术式，偏向直接执行和代码输出

DEFAULT_SYSTEM_PROMPTS = {
    "work": (
        "你是 DSH Work 的工作助手。当前处于 Work 模式，"
        "面向非技术用户和知识工作者。"
        "回答时语言清晰易懂，避免不必要的术语。"
        "产出内容优先使用结构化格式（Markdown 标题、列表、表格）。"
        "处理任务时主动解释你的思路和步骤。"
    ),
    "code": (
        "你是 DSH Work 的开发助手。当前处于 Code 模式，"
        "面向开发者。"
        "直接给出可执行的方案和代码，避免冗长解释。"
        "遵循项目现有代码风格和约定。"
        "修改代码时说明变更原因和影响范围。"
    ),
}

# ===== 空状态快捷入口卡片（第 5.3 节）=====
# 当用户新建会话且尚未发送任何消息时，对话区域展示三个可点击卡片。
# 卡片是纯 UI 组件——三个按钮 + 预定义文本，没有数据源、没有加载逻辑、没有管理界面。

EMPTY_STATE_CARDS = [
    {
        "id": "write_report",
        "title": "写报告",
        "icon": "📝",
        "description": "撰写产品调研、会议纪要等文档",
        "prompt": "请帮我撰写一份关于 [主题] 的文档，包括背景、要点和总结。",
        "mode": "work",
        "color": "purple",
    },
    {
        "id": "analyze_data",
        "title": "分析数据",
        "icon": "📊",
        "description": "处理 CSV/Excel、生成数据周报",
        "prompt": "请分析当前工作区中的数据文件，给出关键指标和趋势分析。",
        "mode": "work",
        "color": "purple",
    },
    {
        "id": "write_code",
        "title": "写代码",
        "icon": "💻",
        "description": "开发功能、调试代码、Git 操作",
        "prompt": "我想开发一个 [功能描述]，请帮我搭建项目结构并实现核心逻辑。",
        "mode": "code",
        "color": "teal",
    },
]

# ===== 首次启动场景选择引导（第 7.3 节）=====
# 环境检测通过后，弹出场景选择界面（全屏弹窗形式的三个卡片）。

SCENARIO_CARDS = [
    {
        "id": "scenario_report",
        "title": "写一份产品调研报告",
        "icon": "📝",
        "prompt": "请帮我撰写一份关于 [产品名] 的市场调研报告，包括竞品分析、用户需求和市场规模评估。",
        "mode": "work",
    },
    {
        "id": "scenario_data",
        "title": "分析这组销售数据",
        "icon": "📊",
        "prompt": "请分析当前工作区 data/ 目录下的销售数据，生成本周销售周报。",
        "mode": "work",
    },
    {
        "id": "scenario_webapp",
        "title": "帮我开发一个网页应用",
        "icon": "💻",
        "prompt": "我想开发一个 [功能描述] 的网页应用，请帮我搭建项目结构。",
        "mode": "code",
    },
]


def get_skills_for_mode(mode: str) -> list[dict]:
    """获取指定模式的默认技能列表。"""
    return DEFAULT_SKILLS.get(mode, [])


def get_system_prompt_for_mode(mode: str) -> str:
    """获取指定模式的默认系统提示词。"""
    return DEFAULT_SYSTEM_PROMPTS.get(mode, DEFAULT_SYSTEM_PROMPTS["work"])


def get_input_placeholder_for_mode(mode: str) -> str:
    """获取指定模式的输入框占位符。"""
    if mode == "code":
        return "输入指令或粘贴代码..."
    return "描述你想完成的工作..."
