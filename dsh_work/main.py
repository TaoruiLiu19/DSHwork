"""DSH Work 应用入口。

用法：
    python -m dsh_work.main
    dsh-work  (安装后)
"""

from __future__ import annotations

import sys


def main() -> int:
    """DSH Work 主入口。

    注意：PySide6 的 shiboken 导入钩子在首次启动时会扫描所有引入模块的源码
    （inspect.getsource），导致 import 阶段可能卡顿 5-10 秒。

    如果用户在此时按下 Ctrl+C，KeyboardInterrupt 会在 import 链中抛出。
    因此 import 语句必须放在 try 块内，确保能被捕获。
    """
    try:
        from .app import DshWorkApp
    except KeyboardInterrupt:
        import sys as _sys
        _sys.stderr.write(
            "\n*** 首次启动需要 5-10 秒加载 PySide6 组件，请不要按 Ctrl+C ***\n"
            "*** 如果多次中断，请运行一次预热命令后再启动：        ***\n"
            "***   python -c \"import dsh_work.ui.panels.usage_panel\"  ***\n\n"
        )
        return 1

    app = DshWorkApp()
    try:
        return app.run()
    except KeyboardInterrupt:
        return 0
    finally:
        app.cleanup()


if __name__ == "__main__":
    sys.exit(main())
