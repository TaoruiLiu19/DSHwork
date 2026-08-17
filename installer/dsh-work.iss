; ===== DSH Work Windows 安装程序（Inno Setup）=====
;
; 构建：
;   1. 先用 PyInstaller 产出 onefile（单 exe）：pyinstaller dsh_work.spec
;   2. 安装 Inno Setup 6（https://jrsoftware.org/isdl.php）
;   3. 命令行编译：iscc installer\dsh-work.iss
;      或用 Inno Setup Compiler IDE 打开本文件按 F7
;
; 产物：installer\Output\DSHWork-Setup-<版本>.exe
;
; 设计要点：
; - 安装到 {autopf}\DSHWork（Program Files，64 位）
; - 用户数据在 ~/.dsh-work/（独立于安装目录），卸载默认保留（用户可手动删）
; - 静默安装支持：DSHWork-Setup-x.y.z.exe /VERYSILENT /CURRENTUSER
; - 仅 64 位（PySide6 + 便携 Node 均为 x64）
; - PyInstaller onefile 模式：dist\DSHWork.exe 单文件，无需 _internal 目录

#define MyAppName "DSH Work"
#define MyAppVersion "0.3.0"
#define MyAppPublisher "DSH Work"
#define MyAppExeName "DSHWork.exe"
#define MyAppURL "https://github.com/deepseek-ai/deepseek-harness"

[Setup]
AppId={{D8F2A1C0-3B47-4E9A-9D6C-1A2B3C4D5E6F}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; PyInstaller onedir 产物在项目根 dist\DSHWork\
SourceDir=..\
OutputDir=installer\Output
OutputBaseFilename=DSHWork-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\{#MyAppExeName}
; 卸载时不删除用户数据（~/.dsh-work 由用户自行决定是否清理）
; SetupIconFile=installer\icons\app.ico   ; 暂无图标，补充后取消注释

[Languages]
; 注：官方 Inno Setup 6 内置语言不含简体中文（ChineseSimplified.isl 为第三方语言包）。
; 如需中文安装向导，请从 https://jrsoftware.org/files/istrans/ 下载后取消下行注释。
; Name: "chinesesimp"; MessagesFile: "compiler:Languages\Unofficial\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller onefile：只有单个 exe，直接打进安装包
Source: "dist\DSHWork.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装完成可选立即启动
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载时若 DSH Work 仍在运行（托盘驻留），先尝试关闭，避免文件占用导致卸载残留
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#MyAppExeName} /F /T 2>nul"; Flags: runhidden; RunOnceId: "KillApp"

[UninstallDelete]
; 仅清理安装目录下的日志缓存，不动 ~/.dsh-work 用户数据
Type: filesandordirs; Name: "{app}\logs"
