# ===== DSH Work 一键构建脚本（PyInstaller onefile + Inno Setup 安装包）=====
#
# 用法（在项目根目录）：
#   .\installer\build.ps1              # 构建 exe + 安装包
#   .\installer\build.ps1 -SkipInstaller   # 仅构建 exe，不编译安装包
#   .\installer\build.ps1 -DebugConsole    # 带控制台（调试 DSH 子进程日志）
#
# 前置：
#   - Python 3.11+（含 PySide6/requests/websockets/portalocker/PyYAML）
#   - Inno Setup 6（仅编译安装包时需要，https://jrsoftware.org/isdl.php）
#
# 产物：
#   dist\DSHWork.exe                         主程序（onefile 单文件）
#   installer\Output\DSHWork-Setup-<ver>.exe  安装包（-SkipInstaller 时跳过）

param(
    [switch]$SkipInstaller,
    [switch]$DebugConsole
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# ---- 1. 准备构建环境（venv 继承系统 Python 的依赖，仅装 pyinstaller）----
$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Step "创建 venv（--system-site-packages，继承系统已装依赖）"
    python -m venv --system-site-packages $venv
}
Write-Step "确认 pyinstaller 已安装"
& $py -m pip install "pyinstaller>=6.0" --quiet 2>$null
& $py -c "import PyInstaller; print('pyinstaller', PyInstaller.__version__)"

# ---- 2. PyInstaller 打包（onefile）----
Write-Step "PyInstaller 打包 dsh_work.spec（onefile 模式）"
$env:PYTHONDONTWRITEBYTECODE = "1"   # 避免向系统 Python 的 __pycache__ 写临时 pyc（沙箱/权限受限时必需）
if ($DebugConsole) { $env:DSHWORK_DEBUG_CONSOLE = "1" }
# 用 `python -m PyInstaller` 模块方式调用：CI 全新环境中 venv 用 --system-site-packages
# 继承系统已装的 pyinstaller 时，venv 里可能没有 Scripts\pyinstaller.exe，模块方式不受影响。
& $py -m PyInstaller dsh_work.spec --noconfirm --log-level WARN
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败 (rc=$LASTEXITCODE)" }

$exe = Join-Path $root "dist\DSHWork.exe"
if (-not (Test-Path $exe)) { throw "未找到产物 $exe" }
Write-Host "EXE 产出: $exe ($([math]::Round((Get-Item $exe).Length/1MB,1)) MB)" -ForegroundColor Green

if ($SkipInstaller) {
    Write-Host "跳过安装包编译（-SkipInstaller）" -ForegroundColor Yellow
    exit 0
}

# ---- 3. Inno Setup 编译安装包 ----
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) {
    # 常见安装路径兜底
    $cands = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $cands) { if (Test-Path $c) { $iscc = $c; break } }
}
if (-not $iscc) {
    Write-Warning "未找到 Inno Setup 编译器 (iscc)。跳过安装包编译。"
    Write-Warning "安装 Inno Setup 6 后运行：iscc installer\dsh-work.iss"
    exit 0
}

Write-Step "Inno Setup 编译安装包"
& $iscc "installer\dsh-work.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败 (rc=$LASTEXITCODE)" }

# 从 dsh-work.iss 读取版本号（单一来源，避免硬编码漂移）
$issMatch = Select-String -Path "installer\dsh-work.iss" -Pattern '^#define MyAppVersion\s+"([^"]+)"'
$issVersion = if ($issMatch) { $issMatch.Matches[0].Groups[1].Value } else { "0.0.0" }
$setup = Join-Path $root "installer\Output\DSHWork-Setup-$issVersion.exe"
if (Test-Path $setup) {
    Write-Host "安装包产出: $setup ($([math]::Round((Get-Item $setup).Length/1MB,1)) MB)" -ForegroundColor Green
    Write-Host "安装包比对: DSHWork.exe 自身 $([math]::Round((Get-Item (Join-Path $root 'dist\DSHWork.exe')).Length/1MB,1)) MB —— 安装包通常比单 exe 小 ~10MB（LZMA2 压缩）" -ForegroundColor Green
}
