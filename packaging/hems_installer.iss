; Inno Setup 安裝精靈設定（優化 E3：Windows 安裝程式）
; 先執行 python packaging/build_installer.py（會呼叫 PyInstaller 再呼叫這個檔），輸出 build/installer/HEMS-Setup-版本.exe
;
; - 預設安裝給目前使用者（%LOCALAPPDATA%\Programs\HEMS），不需要系統管理員權限；安裝時可改為所有使用者
; - 使用者資料（資料庫、記錄、天氣快取）放在 %LOCALAPPDATA%\HEMS，解除安裝時保留，重新安裝後設定還在
; - 繁體中文安裝介面：ChineseTraditional.isl 取自 Inno Setup 官方原始碼庫（jrsoftware/issrc）

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6C1E4B0A-3F2D-4B8E-9C61-HEMS00000001}
AppName=HEMS 居家能源管理系統
AppVersion={#AppVersion}
AppPublisher=Mark1010
AppPublisherURL=https://github.com/Mark666941/hems
DefaultDirName={autopf}\HEMS
DefaultGroupName=HEMS 居家能源管理系統
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\build\installer
OutputBaseFilename=HEMS-Setup-{#AppVersion}
SetupIconFile=..\assets\hems.ico
UninstallDisplayIcon={app}\HEMS.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes

[Languages]
Name: "chinesetraditional"; MessagesFile: "ChineseTraditional.isl"

[Tasks]
Name: "desktopicon"; Description: "建立桌面捷徑"; GroupDescription: "其他選項："

[Files]
Source: "..\build\dist\HEMS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\HEMS 居家能源管理系統"; Filename: "{app}\HEMS.exe"
Name: "{group}\解除安裝 HEMS"; Filename: "{uninstallexe}"
Name: "{autodesktop}\HEMS 居家能源管理系統"; Filename: "{app}\HEMS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\HEMS.exe"; Description: "立即啟動 HEMS"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 解除安裝前結束仍在執行的 HEMS（含計算用的子程序）
Filename: "{cmd}"; Parameters: "/C taskkill /IM HEMS.exe /F"; Flags: runhidden; RunOnceId: "StopHEMS"
