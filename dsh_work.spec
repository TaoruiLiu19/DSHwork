# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onefile 模式，单 exe 分发）。

构建：
    pip install -e ".[dev]"
    pyinstaller dsh_work.spec                      # GUI 模式
    $env:DSHWORK_DEBUG_CONSOLE=1; pyinstaller dsh_work.spec   # 调试：带控制台

产物：
    dist/DSHWork.exe    单文件（约 450–550 MB），双击运行
                        首次启动会解压到 %TEMP%\_MEIXXXXXX 临时目录，
                        启动比 onedir 慢 3–8 秒；关闭程序后临时目录自动删除。

如需切换回 onedir 模式（推荐给安装程序 Inno Setup 打包用），把本文件底部
"onefile 版 EXE" 注释掉、恢复下方注释掉的 onedir EXE+COLLECT 段即可。

设计要点：
- onefile 模式：便于发给终端用户（一个 exe 不拖一堆文件）。
  sys._MEIPASS 指向解压后的临时目录，config.get_builtin_themes_dir() /
  get_builtin_themes_dir 已正确兼容 sys._MEIPASS（config.py#L77 已处理）。
- UPX 关闭：Qt 二进制经 UPX 偶发崩溃 + 杀软误报，得不偿失。
- console 默认 False（GUI 应用）。
"""

from __future__ import annotations

import glob
import os

block_cipher = None

# ===== 资源：内置主题 JSON + 应用图标 =====
# 保持 dsh_work/resources 相对结构，供 get_builtin_themes_dir()/get_builtin_icon_path() 解析命中
datas = [
    (src, "dsh_work/resources/themes")
    for src in glob.glob("dsh_work/resources/themes/*.json")
] + [
    (src, "dsh_work/resources/icons")
    for src in glob.glob("dsh_work/resources/icons/*")
]

# ===== 隐藏导入 =====
# 以下均为函数内惰性 import，PyInstaller 静态分析可能漏掉，显式声明更稳妥：
# - yaml：main_window 设置对话框惰性 import
# - requests：dsh_downloader（便携 Node 下载，无 try/except 保护）+ update_checker 惰性 import
# - portalocker：pid_lock 惰性 import（有降级，但声明更稳）
# - dsh_downloader / update_checker：在 process_manager / app 中惰性 import
hiddenimports = [
    "yaml",
    "requests",
    "portalocker",
    "dsh_work.core.dsh_downloader",
    "dsh_work.core.update_checker",
]

# ===== 体积优化：排除未使用的标准库/第三方包 =====
excludes = [
    "tkinter",
    "test",
    "tests",
    "unittest",
    "pydoc_data",
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
    "PIL",
    "pytest",
    "IPython",
    "jupyter",
    "notebook",
]

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 调试模式：DSHWORK_DEBUG_CONSOLE=1 时带控制台窗口，便于排查 DSH 子进程启动日志
_console = os.environ.get("DSHWORK_DEBUG_CONSOLE") == "1"

# ========== onefile 版：单个 exe，把 binaries/datas/scripts/pyz 全打包进去 ==========
# 注意 onefile 的 EXE 第二个参数必须是 pyz + a.scripts + a.binaries + a.datas + a.zipfiles
# 拼接的完整列表；exclude_binaries 不能用（否则 binaries 会被排除）。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    a.zipfiles,
    [],
    name="DSHWork",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=_console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 应用图标：DeepSeek Harness 黑色小鲸鱼（由 resources/icons/dsh_whale.svg 生成）
    icon="dsh_work/resources/icons/dsh_whale.ico",
)

# ========== onedir 版：产出 dist\DSHWork\（用于 Inno Setup 安装程序打包）==========
# exe = EXE(
#     pyz,
#     a.scripts,
#     [],
#     exclude_binaries=True,
#     name="DSHWork",
#     debug=False,
#     bootloader_ignore_signals=False,
#     strip=False,
#     upx=False,
#     console=_console,
#     disable_windowed_traceback=False,
#     argv_emulation=False,
#     target_arch=None,
#     codesign_identity=None,
#     entitlements_file=None,
#     # icon="resources/icons/app.ico",   # 暂无图标，后续可加
# )

# coll = COLLECT(
#     exe,
#     a.binaries,
#     a.datas,
#     strip=False,
#     upx=False,
#     name="DSHWork",
# )
