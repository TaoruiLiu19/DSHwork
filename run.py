"""PyInstaller 打包入口（顶层脚本）。

dsh_work/main.py 使用包内相对导入（from .app import ...），直接作为
PyInstaller 入口脚本会被当作顶层 main 模块，相对导入因无父包而失败。
本脚本在顶层用绝对导入触发 dsh_work 包初始化，使 main() 内的相对导入生效。

源码运行仍用 `python -m dsh_work`（见 __main__.py），本脚本仅供打包。
"""

from dsh_work.main import main

if __name__ == "__main__":
    raise SystemExit(main())
