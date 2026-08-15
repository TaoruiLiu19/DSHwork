<#
.SYNOPSIS
  DSHWork 项目一键发布到 Gitee。
  用法：
    1) 编辑本脚本顶部的 $GiteeUser / $RepoName / $AuthMode
    2) 右键 → "Run with PowerShell" 执行；或在 PowerShell 里 .\publish-to-gitee.ps1
#>

# ========== 用户必须先填写这三行 ==========
$GiteeUser = ""                  # 例：zhangsan  （https://gitee.com/zhangsan）
$RepoName  = "DSHWork"           # 例：DSHWork   （https://gitee.com/zhangsan/DSHWork）
# 认证方式："SSH"  或  "HTTPS"
#   SSH   : 长期省事，首次需要用本脚本输出的公钥到 Gitee「设置 → SSH 公钥」里添加
#   HTTPS : 推送时要求输入 Gitee 用户名 + 私人令牌（令牌在 https://gitee.com/profile/personal_access_tokens 生成，权限勾 projects/repos/gists/user_info）
$AuthMode  = "SSH"
# ========================================

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host ""; Write-Host "=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  ✅ $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  ⚠ $msg" -ForegroundColor Yellow }
function Err($msg)  { Write-Host "  ❌ $msg" -ForegroundColor Red }
function Prompt($q) { Write-Host -NoNewline "  $q : "; return (Read-Host).Trim() }

# ---------- 1. 检查输入 ----------
Step "1. 校验参数"
if (-not $GiteeUser) { $GiteeUser = Prompt "请输入你的 Gitee 用户名（gitee.com/<用户名>）" }
if (-not $RepoName)  { $RepoName  = Prompt "请输入 Gitee 仓库名（建议 DSHWork）" }
if (-not $GiteeUser -or -not $RepoName) { Err "缺少用户名或仓库名"; exit 1 }
if ($AuthMode -notin @("SSH","HTTPS")) { Err "AuthMode 只能是 SSH 或 HTTPS"; exit 1 }
Ok "目标仓库: https://gitee.com/$GiteeUser/$RepoName   (认证方式: $AuthMode)"

# ---------- 2. 检查 Git ----------
Step "2. 检查 Git 环境"
$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
  # 常见兜底路径
  $cands = @( "${env:ProgramFiles}\Git\cmd\git.exe", "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
              "${env:LOCALAPPDATA}\Programs\Git\cmd\git.exe" )
  foreach ($c in $cands) { if (Test-Path $c) { $env:PATH = (Split-Path $c -Parent) + ";" + $env:PATH; $git = Get-Command git; break } }
}
if (-not $git) {
  Err "未检测到 git.exe。请先安装 Git for Windows：https://git-scm.com/download/win"
  Write-Host "    安装时保持默认勾选项（Add Git to PATH），装完重新打开 PowerShell 即可。"
  exit 1
}
Ok "Git: $(& git --version)"

# ---------- 3. user.name / email ----------
Step "3. Git 身份信息（user.name / user.email）"
$name  = & git config user.name  2>$null
$email = & git config user.email 2>$null
if (-not $name)  { $name  = Prompt "请输入 commit 显示的名字（如 张三）" ; & git config --global user.name  $name }
if (-not $email) { $email = Prompt "请输入 commit 显示的邮箱（随便写一个也行，如 dev@local）"; & git config --global user.email $email }
Ok "user.name=$name   user.email=$email"

# ---------- 4. SSH 或 HTTPS 准备 ----------
$remoteUrl = ""
if ($AuthMode -eq "SSH") {
  Step "4. SSH 公钥准备"
  $sshDir = Join-Path $env:USERPROFILE ".ssh"
  $pub    = Join-Path $sshDir "id_ed25519.pub"
  $prv    = Join-Path $sshDir "id_ed25519"
  if (-not (Test-Path $pub) -or -not (Test-Path $prv)) {
    Warn "未检测到 SSH ed25519 密钥对，开始生成一对（无密码）..."
    New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
    $genExe = "${env:ProgramFiles}\Git\usr\bin\ssh-keygen.exe"
    if (-not (Test-Path $genExe)) { $genExe = (Get-Command ssh-keygen -ErrorAction SilentlyContinue).Source }
    if (-not $genExe) {
      Err "找不到 ssh-keygen.exe（通常随 Git 安装在 Program Files\Git\usr\bin\ssh-keygen.exe）。"; exit 1
    }
    & $genExe -t ed25519 -C "DSHWork@$env:COMPUTERNAME" -N '""' -f $prv | Out-Null
    if (-not (Test-Path $pub)) { Err "生成 SSH 密钥失败"; exit 1 }
    Ok "已生成新的 SSH 密钥对"
  } else { Ok "复用已有 SSH 密钥对: $pub" }
  Write-Host ""
  Write-Host "  ---------- 请复制下面的公钥内容 ----------" -ForegroundColor Yellow
  Get-Content $pub
  Write-Host "  ----------  复制结束  ----------" -ForegroundColor Yellow
  Write-Host ""
  Write-Host "  1) 打开: https://gitee.com/profile/sshkeys"
  Write-Host "  2) 点「添加公钥」，标题随便（如 本电脑），把上面一行公钥粘进去确定"
  $null = Prompt "3) 添加完成后，回到这里按 回车 继续"
  $remoteUrl = "git@gitee.com:$GiteeUser/$RepoName.git"
}
else {
  Step "4. HTTPS 认证准备"
  Write-Host "  访问 https://gitee.com/profile/personal_access_tokens 生成一个私人令牌："
  Write-Host "    - 权限勾选: projects / repos / gists / user_info / groups (可选)"
  Write-Host "    - 过期时间自己定（建议 1 年）"
  Write-Host "    - 生成后 只显示一次，复制保存好"
  $null = Prompt "生成后按 回车 继续（推送时会让你交互输入用户名 + 刚生成的令牌作密码）"
  $remoteUrl = "https://gitee.com/$GiteeUser/$RepoName.git"
}
Ok "远端 URL: $remoteUrl"

# ---------- 5. 本地仓库初始化 + 首次 commit ----------
Step "5. 初始化本地仓库并首次提交"
if (-not (Test-Path ".git")) {
  & git init -q
  Ok "已执行 git init"
} else { Ok "已存在 .git 仓库，复用" }
& git checkout -B main | Out-Null
Ok "切到 main 分支"

Write-Host "  暂存文件（git add .，受 .gitignore 保护）..."
& git add .
$st = (& git status --porcelain)
if ($st) {
  Ok "暂存了 $(($st -split "`n").Count) 个文件，开始 commit..."
  & git commit -q -m "chore: initial commit DSHWork v0.1.0 — DSH 桌面版，支持便携 Node 运行时/安装包/主题/三栏 UI/SM"
  $log1 = (& git log --oneline -1)
  Ok "首次 commit 完成: $log1"
} else {
  $haveCommits = [bool](& git rev-parse --verify HEAD 2>$null)
  if ($haveCommits) { Warn "暂存区为空（工作区无新变更），复用上次 commit: $(& git log --oneline -1)" }
  else {
    # 完全空仓库时做一个空的根提交，便于后续 push
    & git commit --allow-empty -q -m "chore: initial commit"
    Ok "创建空根提交: $(& git log --oneline -1)"
  }
}

# ---------- 6. 配置 remote ----------
Step "6. 配置 Gitee 远端"
$exists = (& git remote 2>$null) -join " "
if ($exists -match "origin") {
  $old = (& git remote get-url origin)
  if ($old -ne $remoteUrl) {
    Warn "origin 已存在且指向: $old"
    Warn "   目标:             $remoteUrl"
    $yn = Prompt "是否切换 origin 到目标仓库？(Y/n)"
    if ($yn -notin @("n","N","no","NO")) {
      & git remote set-url origin $remoteUrl
      Ok "已切换 origin -> $remoteUrl"
    }
  } else { Ok "origin 已是目标地址，无需修改" }
} else {
  & git remote add origin $remoteUrl
  Ok "已添加 origin -> $remoteUrl"
}

# ---------- 7. 先在 Gitee 创建空仓库提醒 ----------
Step "7. 在 Gitee 创建空仓库（必做）"
Write-Host "  现在请新开浏览器打开: https://gitee.com/projects/new"
Write-Host "  创建参数："
Write-Host "    - 所有者: $GiteeUser"
Write-Host "    - 仓库名称: $RepoName"
Write-Host "    - 路径: 默认即可（通常和仓库名相同）"
Write-Host "    - 是否开源: 私有 / 公开，按你需求选"
Write-Host "    - 初始化仓库时  不要  勾选 「使用 Readme / .gitignore / OpenSource License 初始化仓库」"
Write-Host "                     （否则与本地历史冲突，会推送失败）"
$null = Prompt "Gitee 仓库建好后，按 回车 开始推送"

# ---------- 8. 推送 ----------
Step "8. 推送推送到 Gitee (main 分支)"
Write-Host "  执行: git push -u origin main"
Write-Host ""
& git push -u origin main
$rc = $LASTEXITCODE
Write-Host ""

if ($rc -eq 0) {
  Step "9. 发布成功 🎉"
  Ok "仓库主页:  https://gitee.com/$GiteeUser/$RepoName"
  Ok "SSH 克隆:  git clone git@gitee.com:$GiteeUser/$RepoName.git"
  Ok "HTTPS 克隆: git clone https://gitee.com/$GiteeUser/$RepoName.git"
  Write-Host ""
  Write-Host "  后续开发提交只需要："
  Write-Host "    git add ."
  Write-Host "    git commit -m '这里写做了什么'"
  Write-Host "    git push"
} else {
  Step "9. 推送失败排错" -ForegroundColor Red
  Err "git push 返回码: $rc"
  Write-Host ""
  Write-Host "  常见原因与修复："
  Write-Host "  1) 远端非空仓：打开 Gitee 仓库看是不是有 README/.gitignore，有的话删除仓库重建，空仓后再跑本脚本"
  Write-Host "  2) SSH 权限被拒（Permission denied）: 回到第 4 步，确认公钥已复制到 https://gitee.com/profile/sshkeys"
  Write-Host "     验证方法： ssh -T git@gitee.com  （应显示 'Hi <用户名>! You''ve successfully authenticated'）"
  Write-Host "  3) HTTPS 认证失败：检查私人令牌是否过期，推送时密码字段填的是令牌（不是登录密码）"
  Write-Host "  4) 用户名/仓库名拼写错误：核对本脚本顶部的 `$GiteeUser` / `$RepoName` 与 Gitee 仓库 URL 是否一致"
  Write-Host ""
  exit $rc
}
