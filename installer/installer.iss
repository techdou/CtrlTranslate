; CtrlTranslate Inno Setup 安装器（轻量/完整双变体共用一份脚本）
;
; 构建（在仓库根目录）：
;   iscc /DAppVersion=1.5.1 /DVariant=lite installer\installer.iss   → dist\installer\CtrlTranslate-Setup-1.5.1.exe
;   iscc /DAppVersion=1.5.1 /DVariant=web  installer\installer.iss   → dist\installer\CtrlTranslate-Web-Setup-1.5.1.exe
;
; 变体差异：lite 不含 WebEngine（体积小、纯 API 模式）；web 含网页模式。
; 双变体 AppId 不同 → 可共存在不同目录；配置/数据统一在 ~/.ctrltrans，
; 覆盖升级不丢登录态与历史。
;
; 中文语言文件（ChineseSimplified.isl）不在 Inno 官方捆绑清单里——存在则
; 自动启用，不存在回退英文（CI 与本地行为一致，不因语言文件缺失而挂构建）。

#ifndef Variant
#define Variant "web"
#endif

; 版本号由构建方注入（iscc /DAppVersion=x.y.z）；本地直接双击编译时兜底
#ifndef AppVersion
#define AppVersion "0.0.0"
#endif
#if Variant == "lite"
  #define AppName "CtrlTranslate"
  #define AppExeName "CtrlTranslate.exe"
  #define SourceRoot "..\dist\CtrlTranslate"
  #define AppId "{{7A1C4E92-5B3D-4E8F-9C2A-1D6B8E5F4A73}"
#else
  #define AppName "CtrlTranslate Web"
  #define AppExeName "CtrlTranslate-Web.exe"
  #define SourceRoot "..\dist\CtrlTranslate-Web"
  #define AppId "{{C4E9A2F7-8D1B-4A3C-B5E6-F0927D8A1C44}"
#endif

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
