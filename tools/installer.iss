; AI Council — per-user Windows installer (Inno Setup 6)
;
; Build it with:  python tools/build_installer.py
; (that helper produces dist/ai-council/ first, then compiles this script)
;
; Design choices:
;   * install location is per-user (%LOCALAPPDATA%\Programs) so no UAC prompt
;     is needed — this is a local tool, it has no business touching Program Files
;   * the payload is the --onedir PyInstaller build, so launching the app does
;     not unpack anything into %TEMP% on every start
;
; Paths below are relative to this script's directory (tools/).

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "AI Council"
#define AppPublisher "AI Council contributors"
#define AppURL "https://github.com/xmwy0712/ai-council"
#define PayloadDir "..\dist\ai-council"

[Setup]
AppId={{A1C0UNC0-0000-4A11-9E4C-AICOUNCIL0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
OutputDir=..\dist
OutputBaseFilename=AI-Council-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
UninstallDisplayName={#AppName} {#AppVersion}
LicenseFile=..\LICENSE

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\AI Council (Web UI)"; Filename: "{app}\ai-council.exe"; Parameters: "serve"; \
  Comment: "Local web UI at http://127.0.0.1:8765"
Name: "{group}\AI Council (命令行)"; Filename: "cmd.exe"; Parameters: "/K ""{app}\ai-council.exe"" --help"; \
  Comment: "Open a shell with the council CLI"
Name: "{group}\卸载 AI Council"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AI Council (Web UI)"; Filename: "{app}\ai-council.exe"; Parameters: "serve"; \
  Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "在桌面创建快捷方式"; GroupDescription: "附加图标"; Flags: unchecked

[Run]
Filename: "{app}\ai-council.exe"; Parameters: "--help"; Description: "安装后自检"; Flags: nowait postinstall skipifsilent
Filename: "{app}\ai-council.exe"; Parameters: "serve"; Description: "立即启动 AI Council 网页界面"; \
  Flags: postinstall nowait skipifsilent unchecked

[Messages]
ReadyLabel2b=安装完成后即可使用：开始菜单里有「AI Council (Web UI)」，或命令行运行 council。
