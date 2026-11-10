; Inno Setup script for RDR. Built by .github/workflows/release.yml on a
; Windows runner; can also be compiled by hand with ISCC after running
; installer\build_windows.ps1.
;
; Per-user install (no admin prompt): %LOCALAPPDATA%\RDR
; Everything the app writes lives in <install dir>\data and survives
; upgrades and uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef DistDir
  #define DistDir "..\dist\RDR"
#endif

[Setup]
AppId={{7E2A1C0B-5D3F-4B7A-9C11-RDR0000000001}
AppName=RDR
AppVersion={#AppVersion}
AppVerName=RDR {#AppVersion}
AppPublisher=RDR
DefaultDirName={localappdata}\RDR
DefaultGroupName=RDR
DisableProgramGroupPage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableFinishedPage=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=RDR-Setup-{#AppVersion}
SetupIconFile=rdr.ico
UninstallDisplayIcon={app}\rdr.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a Desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start RDR when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; Embedded Python runtime with all requirements pre-installed
Source: "{#DistDir}\python\*"; DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs
; Application code (replaced on upgrade; data\ is not part of the installer)
Source: "{#DistDir}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#DistDir}\run.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\tray.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\VERSION"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\RDR.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "rdr.ico"; DestDir: "{app}"; Flags: ignoreversion
; ffmpeg for the mirror programme feed (optional at build time)
Source: "{#DistDir}\tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[InstallDelete]
; Remove the previous app tree so deleted modules do not linger across upgrades
Type: filesandordirs; Name: "{app}\app"

[Icons]
; Tray application: no console window, icon in the notification area
Name: "{group}\RDR"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\tray.py"""; WorkingDir: "{app}"; IconFilename: "{app}\rdr.ico"; Comment: "Start RDR in the background (tray icon)"
Name: "{group}\RDR (console window)"; Filename: "{app}\RDR.cmd"; WorkingDir: "{app}"; IconFilename: "{app}\rdr.ico"; Comment: "Start RDR with a visible log window"
Name: "{group}\RDR Admin"; Filename: "http://localhost:8080/admin"; IconFilename: "{app}\rdr.ico"
Name: "{group}\Uninstall RDR"; Filename: "{uninstallexe}"
Name: "{autodesktop}\RDR"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\tray.py"""; WorkingDir: "{app}"; IconFilename: "{app}\rdr.ico"; Tasks: desktopicon
Name: "{userstartup}\RDR"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\tray.py"""; WorkingDir: "{app}"; IconFilename: "{app}\rdr.ico"; Tasks: startup

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\tray.py"""; WorkingDir: "{app}"; Description: "Start RDR now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "powershell"; Parameters: "-NoProfile -WindowStyle Hidden -Command ""Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{app}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"""; Flags: runhidden; RunOnceId: "StopRDR"

[Code]
// Stop a running RDR (tray or console) before files are replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec('powershell', '-NoProfile -WindowStyle Hidden -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ''*' + ExpandConstant('{app}') + '*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
