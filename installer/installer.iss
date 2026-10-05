; CtrlTranslate Inno Setup 安装器（单一完整版，含 WebEngine 网页模式）
;
; 构建（在仓库根目录）：
;   iscc /DAppVersion=1.6.0 installer\installer.iss   → dist\installer\CtrlTranslate-Setup-1.6.0.exe
;
; 网页版引擎是应用内模式开关（托盘「启用网页版引擎」），不再拆 lite/Web
; 双变体。配置/数据统一在 ~/.ctrltrans，覆盖升级不丢登录态与历史。
;
; 中文语言文件（ChineseSimplified.isl）不在 Inno 官方捆绑清单里——存在则
; 自动启用，不存在回退英文（CI 与本地行为一致，不因语言文件缺失而挂构建）。

; 版本号由构建方注入（iscc /DAppVersion=x.y.z）；本地直接双击编译时兜底
#ifndef AppVersion
#define AppVersion "0.0.0"
#endif

#define AppName "CtrlTranslate"
#define AppExeName "CtrlTranslate.exe"
#define SourceRoot "..\dist\CtrlTranslate"
#define AppId "{{7A1C4E92-5B3D-4E8F-9C2A-1D6B8E5F4A73}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=techdou
AppPublisherURL=https://github.com/techdou/CtrlTranslate
AppUpdatesURL=https://github.com/techdou/CtrlTranslate/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayName={#AppName}
; 升级安装：程序在托盘运行时覆盖文件会失败——提示用户先退出（程序数据不受影响）
CloseApplications=no
OutputDir=..\dist\installer
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
MinVersion=10.0

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
#if FileExists(CompilerPath + "Languages\ChineseSimplified.isl")
Name: "zh"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceRoot}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
