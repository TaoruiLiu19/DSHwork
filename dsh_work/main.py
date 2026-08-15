"""DSH Work 应用入口。

用法：
    python -m dsh_work.main
    dsh-work  (安装后)
"""

from __future__ import annotations

import sys


def main() -> int:
    """DSH Work 主入口。"""
    from .app import DshWorkApp

    app = DshWorkApp()
    try:
        return app.run()
    except KeyboardInterrupt:
        return 0
    finally:
        app.cleanup()


if __name__ == "__main__":
    sys.exit(main())
